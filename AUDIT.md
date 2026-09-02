# Repository audit — `algo-trading`

**Date:** 2026-08-27  
**Commit:** `a71ae8f` (branch `claude/repo-audit-fu1irg`)  
**Findings:** 82 (14 high, 40 medium, 28 low)

**State reviewed:** 2026-09-02 against `4f68cf4`, under the record conventions
`docs/ROADMAP.md` sets for a file of this kind. 8 closed, 1 half-closed, 73
open; 27 citations no longer resolve. What that review found, and why this file
needed one at all, is §10.

---

## 1. What this is

An audit for inconsistencies, redundancies, and code that does not do what it claims.
It covers the whole repository: `libs/core`, `apps/api`, `apps/worker`, `apps/web`,
`scripts/`, `tests/`, `infra/`, `docs/`, and the root configuration.

**The repository's own quality gates all pass.** Every finding below is something
those gates cannot see — two places that disagree while each is internally consistent,
a guard with a hole in it, a docstring that describes behaviour the code does not have.

| Gate | Result |
|---|---|
| `ruff check` + `ruff format --check` | clean — 280 files |
| `mypy libs apps tests` | clean — 207 source files |
| `pytest tests/unit` | pass — 2,161 test functions |
| `npm run typecheck` / `lint` / `format:check` | clean |
| `vitest` | pass — 259 tests, 16 files |
| `pytest tests/integration` (Redis only) | pass — 26; Postgres tests skipped, no daemon available |

## 2. How to read a finding

Each entry states the claim, the evidence, and the concrete consequence.

Every finding carries two marks, and they answer different questions. The
**evidence** mark says how well the claim was established *when it was written*;
the **state** mark says whether it is still true *today*. A finding can be ✅
Verified and 🟢 Closed at once — well established, and since fixed.

| Evidence | Meaning |
|---|---|
| ✅ **Verified** | I re-read the source myself and confirmed it, in several cases by executing the code. |
| ⚠️ **Reported** | Raised by a subsystem reviewer with file-and-line evidence, but **not** independently re-checked. |

| State | Meaning |
|---|---|
| 🔴 **Open** | The defect is in the tree at the review commit. |
| 🟡 **Half-closed** — @who (#12) | Part of the finding was fixed in that PR; the rest is named in a record note beneath it. |
| 🟢 **Closed** — @who (#12) | Fixed and merged in that PR. Terminal — the finding stays here as history. |

This axis did not exist until §10. It is modelled on `docs/ROADMAP.md`'s four
line states for the same reason that file gives: a record with no terminal state
cannot distinguish work that was done from work nobody has looked at, and reads
as an accusation long after it stopped being one. A state is annotated with the
PR that earned it, in the same diff, exactly as a roadmap tick is.

The adversarial verification pass was **not run** — the audit was wrapped up early, and it
would have spawned one agent per finding at 2-way concurrency. Treat ⚠️ entries as leads with
evidence attached, not as established defects. In my own spot-checks several plausible-looking
claims turned out to be false (see §7), so expect some of the ⚠️ set not to survive scrutiny.

## 3. Summary

### By severity and kind

| | Broken | Inconsistency | Redundancy | Total |
|---|---:|---:|---:|---:|
| 🔴 High | 12 | 2 | 0 | **14** |
| 🟠 Medium | 16 | 22 | 2 | **40** |
| 🟡 Low | 4 | 12 | 12 | **28** |
| **Total** | **32** | **36** | **14** | **82** |

### By state, as at 2026-09-02 (`4f68cf4`)

Derived from the state marks on the findings below and worth nothing if it
disagrees with them, which `tests/unit/test_audit_summary.py` fails the build
over — the same standing `tests/unit/test_roadmap_summary.py` gives the
roadmap's summary. §10.6 said this was missing; it is not any more (#129).

| | 🟢 Closed | 🟡 Half-closed | 🔴 Open | Total |
|---|---:|---:|---:|---:|
| 🔴 High | 1 | 0 | 13 | **14** |
| 🟠 Medium | 6 | 1 | 33 | **40** |
| 🟡 Low | 1 | 0 | 27 | **28** |
| **Total** | **8** | **1** | **73** | **82** |

Of the 73 still open, **50 have never been re-checked by anyone** — they were
⚠️ Reported on 2026-08-27 and are ⚠️ Reported now.

### By area

| Area | High | Medium | Low | Total |
|---|---:|---:|---:|---:|
| docs | 4 | 9 | 4 | 17 |
| apps/api | 1 | 6 | 3 | 10 |
| apps/web (dashboard) | 1 | 3 | 6 | 10 |
| apps/worker | 4 | 2 | 1 | 7 |
| infra / config / CI | 0 | 4 | 3 | 7 |
| libs/core — backtest | 1 | 2 | 3 | 6 |
| scripts | 0 | 4 | 2 | 6 |
| tests | 2 | 1 | 2 | 5 |
| libs/core — data | 1 | 2 | 0 | 3 |
| libs/core — persistence | 0 | 1 | 2 | 3 |
| libs/core — execution | 0 | 2 | 0 | 2 |
| libs/core — risk | 0 | 2 | 0 | 2 |
| libs/core — analytics | 0 | 1 | 0 | 1 |
| libs/core — config | 0 | 1 | 0 | 1 |
| libs/core — errors | 0 | 0 | 1 | 1 |
| libs/core — indicators | 0 | 0 | 1 | 1 |

### The high-severity findings at a glance

| # | Finding | Location | State |
|---:|---|---|---|
| 1 | The dashboard's "close position" never cancels the broker-side stop, despite the module docstring saying it does | `apps/api/src/atp_api/execution.py:87` | 🔴 |
| 2 | The run list and run detail label spec.qty "shares per entry" for every run, including runs the engine never sized by share count | `apps/web/src/components/BacktestRunList.tsx:241` | 🔴 |
| 3 | The queue's interrupted-run sweep is startup-only with a 2-hour threshold, so a normal container restart never sweeps and the row stays `running` forever | `apps/worker/src/atp_worker/queue.py:152` | 🔴 |
| 4 | The live runner marks only open positions, so every entry into a symbol the book does not already hold is refused at sizing | `apps/worker/src/atp_worker/runner.py:712` | 🔴 |
| 5 | Trailing-stop ratchets are computed and then discarded: `_exit_reason` short-circuits on `broker_side`, which is always True in the worker | `apps/worker/src/atp_worker/runner.py:820` | 🔴 |
| 6 | The live runner is pinned to daily bars while the only live writer stores minute bars, so `strategy.on_bar` never fires | `apps/worker/src/atp_worker/trading.py:203` | 🔴 |
| 7 | DASHBOARD.md says every order/position write handler is still a stub; three of them are fully implemented, as DASHBOARD_STATUS.md states | `docs/DASHBOARD.md:808` | 🔴 |
| 8 | DASHBOARD.md states login rate limiting is not built and "nothing slowing down guesses but bcrypt"; the limiter is implemented and wired | `docs/DASHBOARD.md:766` | 🟢 |
| 9 | RISK.md names `flatten_at_close` as one of only two defences against overnight gap risk, but the rule compiler refuses any spec that sets it | `docs/RISK.md:167` | 🔴 |
| 10 | STRATEGY_AUTHORING.md claims the draft→backtesting→paper→live ratchet is "enforced by the API"; every promotion handler is a NotImplementedError stub | `docs/STRATEGY_AUTHORING.md:226` | 🔴 |
| 11 | A stop/target firing on the same bar as a resting exit order leaves the backtest holding a phantom reversed position | `libs/core/src/atp_core/backtest/engine.py:983` | 🔴 |
| 12 | The nightly sweep never re-fetches reconnect-backfilled bars, so "nothing is permanently raw-only" is false and those windows become un-backtestable | `libs/core/src/atp_core/data/stream.py:271` | 🔴 |
| 13 | tests/integration/test_kill_switch.py has no `pytest.mark.integration`, so its 5 tests are deselected by CI and by `make test-integration` | `tests/integration/test_kill_switch.py:28` | 🔴 |
| 14 | `test_money_fields_serialise_as_strings` cannot fail for `unrealized_pnl` or `market_value` — the only test guarding CLAUDE.md §1.1 on the wire is vacuous for nullable fields | `tests/unit/test_api_contract.py:89` | 🔴 |

---

## 4. High severity

These either lose money, prevent the platform from working, or make a documented
safety guarantee untrue.

### apps/api

#### 1. The dashboard's "close position" never cancels the broker-side stop, despite the module docstring saying it does

`apps/api/src/atp_api/execution.py:87` · Broken · 🔴 High · ✅ Verified · 🔴 **Open**

**Evidence**

> apps/api/src/atp_api/execution.py:85-88 claims: "A fresh `StopManager` for the same reason it is fresh in every process: it holds engine-side levels for positions this router did not open, and the ones that matter are broker-side anyway. `flatten` cancels protection **through the venue, not through this object**."
>
> The code does the opposite. build_router (same file, line 98) returns a brand-new `OrderRouter(broker, risk_engine, StopManager(), clock, kill_switch=kill_switch)` per request, so `self._protective` (libs/core/src/atp_core/execution/router.py:222 — `self._protective: dict[str, list[Order]] = {}`) is empty. `OrderRouter.flatten` then does (router.py:738-740):
>
>     result = await self.submit(request, portfolio)
>     if result.submitted:
>         await self.cancel_protection(symbol)
>
> and `cancel_protection` reads only that in-memory dict (router.py:590 — `children = self._protective.get(symbol, [])`), which its own docstring is explicit about (router.py:566-572): "Deliberately narrower than the venue's truth, too — protective orders placed before a restart are not in here, and adopting them is `Reconciler`'s job". With `children == []` the loop body never runs, no `broker.cancel_order` is issued, and it returns 0. `cancel_all()` is the only method that asks the venue what is open, and `flatten` does not call it.

**Why it matters**

An operator clicks "close" on a position the worker opened. The worker placed a GTC broker-side protective stop for that position (`OrderRouter.submit_protective_orders` -> `_stop_order`, router.py:812-849). The API's market close is submitted and fills; the position is now flat and the stop is still resting at the venue. When it triggers it is no longer a closing order — it *opens* a fresh position on the opposite side with nothing protecting it. That is precisely the state `_cancel_stale_protection` logs as "this order now adds to the position instead of closing it" (router.py:786-791) and that `AlpacaBroker.close_all_positions` avoids by passing `cancel_orders=true` (brokers/alpaca.py:504-511). Nothing in the `POST /api/v1/positions/{symbol}/close` path (routers/positions.py:160-256) cancels it, and no test covers it (tests/unit/test_operator_close_out_api.py asserts cancellation only for `/risk/flatten-all`).

**Verification note**

Traced end to end. `build_router` (execution.py:98) returns a fresh `OrderRouter` per request, so `self._protective` (router.py:222) is empty; `flatten` (router.py:738-740) calls `cancel_protection`, which reads only that dict (router.py:590) and therefore issues no broker call. The stop was placed by the worker in a different process. `StopConfig.broker_side` defaults to **True** (stops.py:45), so the venue-side stop is real. `_cancel_stale_protection` is reachable only from `submit_protective_orders` (router.py:424), so cleanup happens only if the worker later re-enters the same symbol.

### apps/web (dashboard)

#### 2. The run list and run detail label spec.qty "shares per entry" for every run, including runs the engine never sized by share count

`apps/web/src/components/BacktestRunList.tsx:241` · Broken · 🔴 High · ⚠️ Reported · 🔴 **Open**

**Evidence**

> BacktestRunList.tsx:241 — `{formatMoney(run.spec.starting_cash, { places: 0 })} · {run.spec.qty} sh/entry`
> BacktestDetail.tsx:379 — `{formatMoney(run.spec.starting_cash, { places: 0 })} · {run.spec.qty} shares per entry ·{' '}`
>
> Neither reads `run.spec.sizing_method` or `run.spec.sizing_value`; `rg 'sizing_method|sizing_value' --glob '*.tsx'` outside the form and tests returns nothing.
>
> The engine ignores `qty` unless the method is fixed_qty — libs/core/src/atp_core/backtest/runner.py:198-203:
>     method = spec.sizing_method or "fixed_qty"
>     ...
>     raw = spec.sizing_value or spec.qty
>
> And the form posts `qty` unconditionally — BacktestForm.tsx:220 `qty,` sits beside `sizing_method: sizingMethod` in the same mutate() payload, while the `qty` state stays at its '100' default whenever the Sizing select is anything but fixed_qty (BacktestForm.tsx:349-373 swaps which input is *rendered*, not what is sent).
>
> The server added these fields to BacktestSpecView specifically so this could not happen — apps/api/src/atp_api/routers/backtests.py:177-181: "Echoed back for the reason the rest of the spec is: a divergence between two runs of one strategy is usually a difference in how they were sized, and a reader comparing them cannot see that unless it travels with the result."

**Why it matters**

Queue a run with Sizing = "Percent of equity" and value 0.05. The engine sizes every entry at 5% of equity; the row and the detail header both say "100 sh/entry". A reader comparing two runs of one strategy that differ only in sizing sees identical text on both, and a reader deciding whether a backtested return is believable is told a share count the run never used — on the two screens the platform provides for reading a stored result before promoting a strategy.

### apps/worker

#### 3. The queue's interrupted-run sweep is startup-only with a 2-hour threshold, so a normal container restart never sweeps and the row stays `running` forever

`apps/worker/src/atp_worker/queue.py:152` · Broken · 🔴 High · ⚠️ Reported · 🔴 **Open**

**Evidence**

> `on_startup` sweeps with `await sweep_interrupted(ctx["runs"], clock.now() - STALE_AFTER, at=clock.now())` (queue.py:152) where `STALE_AFTER = timedelta(seconds=JOB_TIMEOUT_SECONDS * 2)` = 2 hours (queue.py:111). `stale_running` matches only `BacktestRunRow.started_at < older_than` (persistence/backtests.py:214-217). `sweep_interrupted` has exactly one caller — `grep -rn sweep_interrupted` returns only queue.py:152 (the call) and queue.py:171 (the definition) outside tests — and its docstring says "Runs at startup rather than on a schedule" (queue.py:175). Meanwhile docker-compose.yml sets `restart: unless-stopped` on the `queue` service, so the replacement process starts seconds after the old one dies. Concrete scenario: a backtest is 3 minutes into a run when `make deploy` recreates the container (or the process is OOM-killed). The row is left `status='running', started_at = 3 minutes ago`. The new worker starts ~1s later and asks for rows with `started_at < now - 2h`; the row is 3 minutes old, so it is not returned. `MAX_TRIES = 1` and `retry_jobs = False` (queue.py:94, 228) mean arq never redelivers the job, and nothing else in the platform writes a terminal status for a `running` row (`grep STATUS_RUNNING` finds only the repository and `tasks.py:81`).

**Why it matters**

The row is stranded at `running` permanently — the exact outcome the module's own comments call "the worst outcome this queue can produce" (queue.py:106-108) and "a run stuck at 'running' forever is the worst outcome for a user" (tasks.py:55). The Backtests tab shows a spinner that never resolves, and the sweep that is supposed to correct it can only fire if the queue worker happens to stay down for more than two hours — the one case where an operator has already noticed. The threshold's stated justification ("so a run that is merely slow is never swept out from under itself", queue.py:109-110) is reasoning for a periodic sweep, but this sweep runs only at process start, when `MAX_JOBS = 1` guarantees no run of this worker is legitimately in flight.

#### 4. The live runner marks only open positions, so every entry into a symbol the book does not already hold is refused at sizing

`apps/worker/src/atp_worker/runner.py:712` · Broken · 🔴 High · ✅ Verified · 🔴 **Open**

**Evidence**

> `StrategyRunner._mark` (runner.py:702-729) is the only thing that ever writes `last_price` onto the live portfolio, and it starts:
>
>     open_symbols = [p.symbol for p in portfolio.open_positions]
>     if not open_symbols:
>         return
>
> A flat symbol therefore never gets a mark. `risk/rules.py:reference_price` (the single pricing function sizing and the rules share) is:
>
>     if limit_price is not None:
>         return limit_price
>     position = portfolio.positions.get(symbol)
>     if position is not None and position.last_price is not None:
>         return position.last_price
>     return None                                  # rules.py:69
>
> `OrderRouter._size` (router.py:1163-1171) then does:
>
>     price = reference_price(signal.symbol, portfolio, signal.limit_price)
>     if price is None:
>         return SubmitResult.refused(SIZING, f"no price available for {signal.symbol}: nothing has marked it and the signal carries no limit price")
>
> The shipped strategies emit no limit price (`sma_crossover.py:75-84` constructs `Signal(...)` with no `limit_price`, so `OrderType.MARKET` at router.py:290) and only signal an entry when `position.is_flat` (sma_crossover.py:73). Verified directly: `reference_price("AAPL", Portfolio(cash=100000, starting_equity=100000), None)` returns `None`, and still returns `None` after `portfolio.position("AAPL")` (the get-or-create a strategy's `ctx.position()` performs).
>
> This disagrees with the backtest, which marks every symbol that printed a bar whether held or not — `backtest/engine.py:629`: `self._portfolio.position(symbol).last_price = bar.close`. Nothing in `warmup` (runner.py:355-437) closes the gap either; it fills `self._bars`, not the portfolio's marks.

**Why it matters**

The live/paper worker cannot open a position at all. Every ENTER_LONG/ENTER_SHORT a default-configured strategy emits is refused at the SIZING stage before any risk rule runs, while the same strategy over the same bars trades normally in a backtest — exactly the live-vs-backtest divergence docs/RISK.md:55-59 ("Two callers, one function… a backtest that sized differently would report a return the live strategy could not reproduce") exists to prevent. The symptom is docs/FIRST_PAPER_RUN.md's "a week of no signals is not a week of correct trading": a worker that runs perfectly and fills nothing. `worker/preflight.py:check_sizing_is_reachable` — the check written specifically to catch a silent week — passes, because it calls `position_size` with a price handed to it rather than asking whether the portfolio could produce one.

**Verification note**

Reproduced directly. `_mark` iterates `portfolio.open_positions` only, so a symbol never held has no `last_price`. Executed: `reference_price('SPY', Portfolio(...), None)` returns `None`, and `_size_for_signal` (router.py:1163) refuses at `SIZING` on `None`. No shipped example strategy sets `limit_price` and `Signal.limit_price` defaults to `None` (signal.py:37), so every first market entry is refused. It survives because `test_strategy_runner.py` drives a router double (`router.refuse_signals`), never the real router.

#### 5. Trailing-stop ratchets are computed and then discarded: `_exit_reason` short-circuits on `broker_side`, which is always True in the worker

`apps/worker/src/atp_worker/runner.py:820` · Broken · 🔴 High · ✅ Verified · 🔴 **Open**

**Evidence**

> `_check_stops` ratchets the level (runner.py:752-759):
>
>     if self.stop_config.stop_type in (StopType.TRAILING_PCT, StopType.CHANDELIER):
>         atr = self._atr(position.symbol)
>         moved = self.stop_manager.update_trailing(position, bar, self.stop_config, atr)
>         if moved is not None:
>             log.info("runner.trailing_stop_ratcheted", ...)
>
> `update_trailing` mutates `position.stop_loss_price` only (stops.py:206). `_exit_reason` is then asked whether to close, and returns before ever consulting it (runner.py:820-827):
>
>     if self.stop_config.broker_side:
>         # The *stop* is resting at the venue; checking it here as well would double-exit …
>         return TAKE_PROFIT if target_hit(position, bar) else None
>
>     if self.stop_manager.should_trigger(position, bar):   # runner.py:829 — unreachable in the worker
>
> `StopConfig.broker_side` defaults to True (stops.py:45) and `trading.py:resolve_stop_config` (line 229) never sets it, so the worker's config is always `broker_side=True`. Nothing amends the resting venue order either: `submit_protective_orders` is called once per entry fill and `OrderRouter` has no replace/amend method (grep for `replace_order|amend|modify_order` across `libs/` and `apps/` returns nothing).

**Why it matters**

With `WORKER_STOP_TYPE=trailing_pct` or `chandelier` — both accepted values of the `Literal` in `config.py:116` — the trailing behaviour does not exist. The venue holds the entry-time stop forever, the ratcheted level lives only on the in-memory position, and `should_trigger` is never called, so a long that runs from 100 to 140 is still protected at its original level when it reverses. This contradicts stops.py:13-16 and docs/RISK.md:84-86 ("Broker-side stops for live positions… Engine-side logic layers on top to tighten it") and router.py:392 ("the armed level is … the value a trailing stop ratchets"). The only visible trace is a `runner.trailing_stop_ratcheted` INFO line that implies protection moved when it did not — the precise failure `update_trailing`'s own docstring warns about: "a stop ends up computed but never armed".

**Verification note**

Confirmed, and worse than stated. `update_trailing` (runner.py:754) moves the level in memory and logs `runner.trailing_stop_ratcheted`, but `rg` finds no `replace_order`, `amend` or `modify_order` anywhere in the repository — there is no way to move a resting venue order. So with the default `broker_side=True` the ratchet reaches neither the venue nor `_exit_reason`, and the log line claims a ratchet that has no effect.

#### 6. The live runner is pinned to daily bars while the only live writer stores minute bars, so `strategy.on_bar` never fires

`apps/worker/src/atp_worker/trading.py:203` · Broken · 🔴 High · ✅ Verified · 🔴 **Open**

*Record note (§10, 2026-09-02): Cited `:185` on 2026-08-27; the code is at `:203` today.*

**Evidence**

> `build_runner` hard-codes the runner's series: `timeframe=Timeframe.D1,` (trading.py:185) — there is no `WORKER_TIMEFRAME` setting in `atp_core.config.Settings` to change it. The only process writing bars during a session is the ingestor, and `main.py:164` builds `StreamIngestor(...)` without `bar_timeframe`, so it uses the default `bar_timeframe: Timeframe = Timeframe.M1` (libs/core/src/atp_core/data/stream.py:104) and `_handle_bar` writes those M1 rows (stream.py:245). `PostgresBarRepository.get_last_n_bars` is a plain `select(BarRow).where(..., BarRow.timeframe == timeframe.value)` (persistence/bars.py:161-166) — no rollup from M1 to D1. The only writers of `1d` rows are `scripts/backfill_bars.py`, `scripts/seed.py`, and `scheduler.backfill_missing_bars`, which is scheduled `{"job": backfill_missing_bars, "trigger": "cron", "hour": 2, "minute": 0}` (scheduler.py:239) — 02:00 UTC, market shut. Now the mechanism: `warmup` REPLACES the window with the newest stored bars — `bars = await self.bar_repo.get_last_n_bars(symbol, self.timeframe, needed)` / `self._bars[symbol] = list(bars)` (runner.py:380-381) — and `run()` re-runs `warmup` at every session open. `_refresh_bars` then only reports a bar it has not already got: `if held and bar.ts <= held[-1].ts: continue` (runner.py:691-692). Since warmup has just set `held[-1]` to the newest stored `1d` bar and nothing writes another `1d` row until 02:00 the next morning (when the runner is asleep in `_sleep_until_open`), `just_closed` is `[]` on every pass, forever. The unit suite only passes because it fakes the condition: `close_bar()`'s own docstring in tests/unit/test_strategy_runner.py:305-310 says "`warmup` loads the *most recent* bars, so a runner handed its whole series up front has nothing left to close and never calls the strategy. Tests warm up on the history and then close the next bar through here."

**Why it matters**

In the deployed configuration the live strategy loop never calls `strategy.on_bar` and never generates a signal — `_evaluate_once` step 4 (`self._poll_strategy(closed)`) always receives an empty list. `_check_stops` is driven off the same list (`by_symbol = {bar.symbol: bar for bar in closed}`, runner.py:743), so trailing-stop ratcheting, time exits and take-profit exits never run either. The failure is invisible: `runner.evaluations` climbs, the log is clean, and docs/FIRST_PAPER_RUN.md line 206 tells the operator "`sma_crossover` on daily bars signals rarely. Do not interpret silence as breakage" — so a paper week of total silence reads as the documented expected state. That is precisely the unattributable silent week `atp_worker.preflight` was written to prevent, and it costs a week of calendar time to discover.

**Verification note**

Confirmed. `StreamIngestor` is constructed at `main.py:164` without `bar_timeframe`, so it takes the default `Timeframe.M1` (stream.py:104) and stores minute bars. The runner is built with `timeframe=Timeframe.D1` (trading.py:185) and reads via `bar_repo.get_last_n_bars(symbol, self.timeframe, ...)` (runner.py:380, 687). The only live writer and the only live reader therefore use different timeframes.

### docs

#### 7. DASHBOARD.md says every order/position write handler is still a stub; three of them are fully implemented, as DASHBOARD_STATUS.md states

`docs/DASHBOARD.md:808` · Inconsistency · 🔴 High · ⚠️ Reported · 🔴 **Open**

*Record note (§10, 2026-09-02): Cited `:247` on 2026-08-27; the code is at `:808` today.*

**Evidence**

> docs/DASHBOARD.md:247 "**Only the read is built.** `POST /orders`, `DELETE /orders/{id}` and `/orders/cancel-all` are still stubs"; :296-298 "`POST /positions/{symbol}/close` and `PATCH /positions/{symbol}/stop` are still stubs"; :683 "Every write handler across `orders.py` and `positions.py` is still a stub".
>
> Only three handlers actually raise: apps/api/src/atp_api/routers/orders.py:243 (`POST /orders`), positions.py:157 (`GET /{symbol}`), positions.py:273 (`PATCH /{symbol}/stop`). `DELETE /orders/{order_id}` (orders.py:246), `POST /orders/cancel-all` (orders.py:336, "Through `OrderRouter.cancel_all`") and `POST /positions/{symbol}/close` (positions.py:160, "Through `OrderRouter.flatten`") are all implemented with real dependencies and audit sinks.
>
> docs/DASHBOARD_STATUS.md:236-239 says the opposite and is correct: "`DELETE /orders/{id}` and `POST /orders/cancel-all` are built"; :187 "`POST /{symbol}/close` is built and goes through the risk chain". libs/core/src/atp_core/audit/ports.py documents ORDER_CANCELLED and POSITION_CLOSED as verbs written by those very handlers.

**Why it matters**

docs/RUNBOOK.md:154 tells an operator handling runaway order submission to `POST /api/v1/orders/cancel-all`. A reader who checked DASHBOARD.md first would believe that endpoint is a stub and reach for the broker UI instead, during the incident where seconds matter. Two docs in the same directory give opposite answers about which emergency endpoints exist.

#### 8. DASHBOARD.md states login rate limiting is not built and "nothing slowing down guesses but bcrypt"; the limiter is implemented and wired

`docs/DASHBOARD.md:766` · Inconsistency · 🔴 High · ✅ Verified · 🟢 **Closed** — @claude (#113)

*Record note (§10, 2026-09-02): Cited `:713` on 2026-08-27; the code is at `:766` today.*

**Evidence**

> docs/DASHBOARD.md:704 "**Sign-in and scopes exist; rate limiting and revocation do not.**" and :713 "What is *not* built: any rate limit on the login endpoint, revocation before a session expires…"; also :651-655 "What is still absent is everything around it: no rate limit on the login endpoint… there is nothing slowing down guesses at it but bcrypt."
>
> apps/api/src/atp_api/routers/auth.py:21-22, :86, :104-109 — the login handler takes `limiter: Annotated[RateLimiter, Depends(get_rate_limiter)]`, keys on `f"ratelimit:login:{address}"` and logs `auth.login_rate_limited`. libs/core/src/atp_core/config.py:167-168 declares `api_login_attempts = 10` / `api_login_window_seconds = 300`.
>
> Contradicted by docs/adr/0010-rate-limiting-and-the-audit-trail.md ("Rate limiting, on the unauthenticated surface only… A fixed-window counter in Redis, keyed on the client address, applied to `/auth/login`"), by docs/SAFETY.md:172 ("Ten attempts per five minutes per client address") and by docs/ROADMAP.md:2118, which ticks the item.

**Why it matters**

Three docs and the roadmap say the login brute-force control is built and ticked; DASHBOARD.md says it is missing, in the section whose whole job is telling an operator whether the dashboard may face a network. A reader deciding where to bind the stack gets opposite answers depending on which page they open, and the doc that understates the defence is the one that also carries the bind-address guidance.

**Verification note**

Confirmed. `auth.py:86` injects a `RateLimiter` dependency and `auth.py:103` calls `limiter.check(f"ratelimit:login:{address}")` before verifying the password; `api_login_attempts` and `api_login_window_seconds` are live `Settings` fields. The doc says the opposite — and so does the handler’s own docstring at `auth.py:91` (‘There is no rate limit here yet’), which is the separate finding below.

#### 9. RISK.md names `flatten_at_close` as one of only two defences against overnight gap risk, but the rule compiler refuses any spec that sets it

`docs/RISK.md:167` · Broken · 🔴 High · ⚠️ Reported · 🔴 **Open**

**Evidence**

> docs/RISK.md:165-167 ("What stops cannot do"): "Overnight gap risk is real, and the only defences are position size and `flatten_at_close` — not tighter stops."
>
> libs/core/src/atp_core/strategy/rules.py:459-464, inside `_refuse_what_cannot_run`:
>     if spec.risk.flatten_at_close:
>         raise InvalidRuleError(
>             "flatten_at_close is not modelled yet: a strategy cannot see the "
>             "session end (it never reads the clock, CLAUDE.md §1.5) and guessing "
>             "one would exit at a different bar in a backtest than in production"
>         )
> Confirmed by tests/unit/test_rule_compilation.py:486 `test_flatten_at_close_is_refused_rather_than_ignored`. docs/STRATEGY_AUTHORING.md:206 and docs/ROADMAP.md:1006 both correctly list `flatten_at_close` among the things compilation refuses — RISK.md is the only page that still presents it as a control.

**Why it matters**

An operator reading the platform's risk specification is told that a strategy holding overnight can be protected by setting `flatten_at_close`. Setting it makes the rule set fail to compile, so the strategy cannot run at all; and because the doc names only two defences, a reader who drops it is left believing position size is a deliberate second layer when it is in fact the only one. RISK.md is the specification the risk layer is implemented against (docs/RISK_IMPLEMENTATION_NOTES.md says so in its opening paragraph), so this is the spec disagreeing with the enforcement.

#### 10. STRATEGY_AUTHORING.md claims the draft→backtesting→paper→live ratchet is "enforced by the API"; every promotion handler is a NotImplementedError stub

`docs/STRATEGY_AUTHORING.md:226` · Broken · 🔴 High · ✅ Verified · 🔴 **Open**

**Evidence**

> docs/STRATEGY_AUTHORING.md:222-227:
>     draft → backtesting → paper (≥4 weeks) → live
>     Each gate is enforced by the API. See SAFETY.md for what live additionally requires.
>
> apps/api/src/atp_api/routers/strategies.py:674-698 `promote_strategy` ends in `raise NotImplementedError`, and its own docstring says "Still a stub… What is left is this endpoint's own work, and it is not small: a verb per transition, the minimum paper-trading period measured against something (nothing today records when a strategy reached `paper`), and the refusal to move more than one rung at a time." strategies.py:701-705 `pause_strategy` likewise `raise NotImplementedError`. libs/core/src/atp_core/persistence/strategies.py:87 and :119 show every writer stores `state=StrategyState.DRAFT` — no code path advances a strategy past `draft`. docs/ANALYTICS.md:163-165 and docs/BACKTESTING.md ("Nothing records that yet (ADR 0010's lifecycle verbs)") both state the opposite of STRATEGY_AUTHORING.md.

**Why it matters**

A strategy author is told the platform will stop them promoting straight to live without a backtest and four paper weeks. No such gate exists — the only thing standing between a `draft` row and real orders is the env flags in SAFETY.md, which this sentence explicitly defers to as an *additional* requirement on top of a ratchet that is not there. The doc converts an unbuilt control into a believed one, on the exact path where believing it is expensive.

**Verification note**

Confirmed. `STRATEGY_AUTHORING.md:226` reads ‘Each gate is enforced by the API.’ Both handlers that would enforce it are bare stubs: `promote_strategy` (strategies.py:675) and `pause_strategy` (strategies.py:705) are each `raise NotImplementedError`, and I confirmed by probe that they are published, callable routes.

### libs/core — backtest

#### 11. A stop/target firing on the same bar as a resting exit order leaves the backtest holding a phantom reversed position

`libs/core/src/atp_core/backtest/engine.py:983` · Broken · 🔴 High · ⚠️ Reported · 🔴 **Open**

**Evidence**

> The per-bar order is stops-then-fills (engine.py:638-639):
>
>     self._check_stops(bar, result)  # 3
>     self._fill_pending_for(bar, result)  # 4
>
> `_check_stops` closes the whole position (`qty=abs(position.qty)`) and `_fill_pending_for` then executes any order resting from the previous bar with no check that the position it was sized against still exists:
>
>     price = self._intended_price(order, bar)
>     ...
>     self._execute(order, bar, price, result)   # engine.py:983
>
> `_execute` calls `position.apply_fill(fill, qty * order.side.sign)` unconditionally (engine.py:1035), and `Position.apply_fill` opens a new position from flat.
>
> Reproduced against the real engine (Scripted strategy, ZeroCostModel, all-approving risk engine, entry with stop_loss=95; bar 3 low=90 breaches it while a signal EXIT queued on bar 2 is still resting):
>
>     orders:
>       entry      buy  qty=10 filled=10 @ 100
>       exit       sell qty=10 filled=10 @ 100
>       stop_loss  sell qty=10 filled=10 @ 95
>     final position qty = -10  (0 expected)
>     open positions: ['TEST']
>
> The repro script is at /tmp/claude-0/-home-user-algo-trading/60825347-c05b-58e3-a29a-ddb5ba89b356/scratchpad/repro.py

**Why it matters**

Any run that arms stops (`--stop atr`, or a strategy that emits `stop_loss_price`) and whose strategy also emits EXIT signals can end up short a position it never asked for, the moment a stop or take-profit triggers on the bar immediately after an exit signal. The phantom position is then marked every bar, counted in `open_positions`, priced into `unrealized_pnl`/`ending_equity`, aged by `_bars_held`, and re-armed by `_arm_from_config` — so `totals()` and the equity curve report P&L from a position the strategy never held. The docs (docs/BACKTESTING.md, 'Reading the result') tell the reader to trust `realized_pnl`/`unrealized_pnl` as the split between banked and marked; here part of the mark belongs to an order that should have been dropped as already-filled.

### libs/core — data

#### 12. The nightly sweep never re-fetches reconnect-backfilled bars, so "nothing is permanently raw-only" is false and those windows become un-backtestable

`libs/core/src/atp_core/data/stream.py:271` · Broken · 🔴 High · ✅ Verified · 🔴 **Open**

**Evidence**

> stream.py:266-272 (`_backfill_gap` docstring): "Raw prices, not adjusted. ... The nightly sweep re-fetches the same range adjusted, so nothing is permanently raw-only." — and the call at stream.py:314-322 passes `adjusted=False`.
>
> docs/DATA.md:124-126 repeats it verbatim: "Bars fetched this way are **raw, not adjusted**. ... the nightly sweep re-fetches the same range adjusted, so nothing stays raw-only."
>
> But the only nightly sweep is `atp_worker.scheduler.backfill_missing_bars` (scheduler.py:168), which calls `backfill_gaps`, and `backfill_gaps` fetches *only what is missing*:
>
>     backfill.py:309   gaps = await repository.find_gaps(symbol, timeframe, start, end)
>     backfill.py:311   if not gaps:
>     backfill.py:312       continue
>     backfill.py:325   for gap_start, gap_end in attempted:
>
> `find_gaps` (persistence/bars.py:171-232) reports a window only when *no bar is stored* for it. The reconnect backfill has just stored bars there, so those windows are not gaps and are never re-requested. `upsert_bars` also COALESCEs (`bars.py:106`), so nothing else can fill the column later; `apply_corporate_actions` (scheduler.py:205) is still `raise NotImplementedError`.
>
> The consequence is enforced elsewhere: `backtest/engine.py:797-810` raises `UnadjustedDataError` for any symbol with a bar whose `adj_close` is None (per docs/adr/0017).

**Why it matters**

Every bar written by a `FeedReconnected` gap fill keeps `adj_close = NULL` forever. The moment anyone backtests a range that contains a reconnect, `BacktestEngine.run` refuses the whole run with `UnadjustedDataError` naming the symbol — and the operator's only repair is a manual `scripts/backfill_bars.py` over the range, which neither the error nor the docs tell them is needed because both state the nightly job already did it. Either the sweep has to re-fetch stored-but-unadjusted rows, or the two claims (stream.py:271-272, docs/DATA.md:126) have to be withdrawn.

**Verification note**

Confirmed. The nightly sweep is `backfill_missing_bars` (scheduler.py:118), which drives `find_gaps` — it looks for *missing rows*. A reconnect-backfilled bar exists as a row with `adj_close` NULL, so no gap is reported and it is never re-fetched. Nothing anywhere queries for a NULL `adj_close`, and `backtest/engine.py:800` refuses any run containing one.

### tests

#### 13. tests/integration/test_kill_switch.py has no `pytest.mark.integration`, so its 5 tests are deselected by CI and by `make test-integration`

`tests/integration/test_kill_switch.py:28` · Broken · 🔴 High · ✅ Verified · 🔴 **Open**

**Evidence**

> The module goes straight from imports to constants with no marker:
>
> ```
> 25 if TYPE_CHECKING:
> 26     from collections.abc import Iterator
> 27
> 28 PREFIX = "atp:test:halt"
> ```
>
> Every other file in tests/integration/ carries one, e.g. tests/integration/test_redis_adapters.py:34 `pytestmark = pytest.mark.integration` (also test_ws_bridge.py:40, test_migrations.py:29, test_backup_restore.py:38, test_order_persistence.py:40, test_backtest_runs.py:43, test_audit_log.py:28, test_seed.py:45, test_bar_repository.py:25, test_decision_record.py:48). `rg -n "pytestmark|@pytest.mark" tests/integration/test_kill_switch.py` returns nothing, and tests/integration/conftest.py adds no marker in collection either.
>
> Verified by collection:
> ```
> $ pytest tests/integration -m integration --collect-only -q
> tests/integration/test_audit_log.py: 8
> ... 10 files listed, test_kill_switch.py absent ...
> $ pytest tests/integration/test_kill_switch.py --collect-only -q
> tests/integration/test_kill_switch.py: 5
> ```
>
> The command that filters them out is the only integration command the project has: .github/workflows/ci.yml `run: uv run pytest tests/integration -m integration`, and Makefile `test-integration: uv run pytest tests/integration -m integration`.

**Why it matters**

The kill switch is docs/SAFETY.md layer 6 and the file's own docstring says these five tests exist to cover the properties the unit tests structurally cannot — that the API process can trip the halt while the worker is mid-loop, that it survives a worker restart, and that it fails closed against a genuinely unreachable Redis. None of them has ever executed in CI: the marker filter drops the file before collection, and `make test-integration` drops it too. A regression in RedisKillSwitch (a changed key prefix, a lost cross-connection read, a fail-open on ConnectionError) would go green on every PR and every merge. The only command that would run them is a bare `pytest` with REDIS_URL exported locally, which nothing in the Makefile or CI does.

**Verification note**

Confirmed by collection. `pytest tests/integration --collect-only` gathers 196 tests; `pytest tests/integration -m integration` — what CI and `make test-integration` both run — gathers **191**. The 5 missing are exactly this file, the only one in the directory without the marker.

#### 14. `test_money_fields_serialise_as_strings` cannot fail for `unrealized_pnl` or `market_value` — the only test guarding CLAUDE.md §1.1 on the wire is vacuous for nullable fields

`tests/unit/test_api_contract.py:89` · Broken · 🔴 High · ⚠️ Reported · 🔴 **Open**

**Evidence**

> ```python
> 87    for field in ("qty", "avg_entry_price", "unrealized_pnl", "market_value"):
> 88        prop = position["properties"][field]
> 89        assert prop.get("type") != "number", (
> 90            f"PositionView.{field} serialises as a JSON number; it must be a string"
> 91        )
> ```
>
> `PositionView.market_value` and `.unrealized_pnl` are `Decimal | None` (apps/api/src/atp_api/routers/dashboard.py:102-103), which Pydantic renders as an `anyOf` with **no top-level `type` key**. From the committed apps/web/openapi.json:
>
> ```
> market_value {"anyOf": [{"pattern": "...", "type": "string"}, {"type": "null"}], "title": "Market Value"}
> unrealized_pnl {"anyOf": [{"pattern": "...", "type": "string"}, {"type": "null"}], "title": "Unrealized Pnl"}
> ```
>
> So `prop.get("type")` is `None`, and `None != "number"` is always true. The float case produces the same shape — verified against the installed pydantic:
>
> ```
> class M(BaseModel): market_value: float | None
> # -> {"anyOf": [{"type": "number"}, {"type": "null"}], "title": "Market Value"}
> ```
>
> no top-level `"type"` there either, so the assertion still passes. Only the two non-nullable fields (`qty`, `avg_entry_price`, which render as a bare `{"type": "string"}`) are actually checked.
>
> Second, smaller inconsistency two lines above: line 84-85 `if position is None:  # pragma: no cover - router not implemented yet` / `pytest.skip("PositionView not in the schema yet")` is stale dead code — `PositionView` is defined at dashboard.py:88 and is in the schema; the full unit run shows no skips.

**Why it matters**

`rg` over tests/ finds exactly one assertion of this kind in the whole suite, so this test is the sole automated guard that money does not cross the wire as a JSON number (CLAUDE.md §1.1, and the reason apps/web/src/lib/money.ts refuses to parse). Change `market_value: Decimal | None` to `float | None` on a nullable field and every gate stays green: mypy accepts it, `make check` passes, and the dashboard starts rendering a float-rounded market value and unrealised P&L — the exact figures docs/DASHBOARD.md says must never come from a float. The test's own failure message names those two fields, so a reader reasonably believes they are covered.

---

## 5. Medium severity

Real defects and disagreements that mislead an operator or a contributor, but that
do not by themselves break trading.

### apps/api

#### 15. `_realised_curve` anchors the curve at the *mean* trade notional while its docstring says the *summed* magnitude

`apps/api/src/atp_api/routers/analytics.py:368` · Inconsistency · 🟠 Medium · ⚠️ Reported · 🔴 **Open**

**Evidence**

> Docstring (analytics.py:360-362): "Anchored at the summed magnitude of the period's own trades instead: a notional stake large enough that the ratios are proportions of what was risked..."
> Code (analytics.py:368):
> ```python
> stake = sum((abs(t.entry_price * t.qty) for t in trades), Decimal(0)) / len(trades)
> ```
> The `/ len(trades)` makes it the average notional, smaller than the documented anchor by a factor of the trade count. The anchor is `curve[0]`, which `PerformanceAnalyzer.metrics` (libs/core/src/atp_core/analytics/performance.py:337) passes straight into `compute_all` as the equity series.

**Why it matters**

`total_return`, `cagr` and `max_drawdown` on `GET /analytics/performance` and on the live side of `/analytics/live-vs-backtest` are all fractions of this anchor, so they come out roughly N times larger than the docstring's definition and they change scale with the number of trades in the window. A reader reconciling a live total_return against a backtest's — which is what the live-vs-backtest report exists for — cannot get the same number from the stated definition.

#### 16. `login`'s docstring says there is no rate limit, in a handler whose first action is the rate limit

`apps/api/src/atp_api/routers/auth.py:91` · Inconsistency · 🟠 Medium · ✅ Verified · 🟢 **Closed** — @claude (#113)

**Evidence**

> The docstring says: "There is no rate limit here yet — that is its own Phase 6 item. What stands in for one meanwhile is bcrypt itself ... It is a brake, not a lock, and the item above it in the roadmap is the lock." Twelve lines below, the same function does:
> ```python
> verdict = await limiter.check(
>     f"ratelimit:login:{address}",
>     settings.api_login_attempts,
>     settings.api_login_window_seconds,
> )
> if not verdict.allowed:
>     ... raise HTTPException(429, ...)
> ```
> ADR 0010 (`docs/adr/0010-rate-limiting-and-the-audit-trail.md`) records the limiter as Accepted and built, and docs/SAFETY.md:158 states "Sign-in is rate limited".

**Why it matters**

This is the one docstring a maintainer reads before touching the login path. It says the lock is missing and points at a roadmap item that is already done, which invites either a second, duplicate limiter on the same endpoint or a security review that concludes the gap is still open. It also contradicts the module the handler calls (`atp_api/ratelimit.py`), ADR 0010 and docs/SAFETY.md.

**Verification note**

Confirmed — the docstring and the code it documents are eleven lines apart and disagree. `auth.py:91` says ‘There is no rate limit here yet — that is its own Phase 6 item’; `auth.py:103` is the limiter call.

#### 17. Thirteen unbuilt endpoints are published in the OpenAPI schema and return HTTP 500, not 501

`apps/api/src/atp_api/routers/marketdata.py:29` · Broken · 🟠 Medium · ✅ Verified · 🔴 **Open**

**Evidence**

> Thirteen route handlers are a bare `raise NotImplementedError`: `marketdata.get_bars/get_quote/search_symbols`, `strategies.list_available_strategy_classes/get_strategy/update_strategy/promote_strategy/pause_strategy`, `dashboard.get_system_health`, `analytics.daily_report`, `orders.submit_manual_order`, `positions.get_position/update_stop`. `apps/api/src/atp_api/main.py` registers no exception handler for `NotImplementedError`. Probed with `TestClient` and an overridden `get_current_session`: ten return 500 outright, the other three (`submit_manual_order`, `update_stop`, `promote_strategy`) return 422 only because an empty body fails validation first. All thirteen appear in `app.openapi()['paths']` and therefore in the generated `apps/web/src/api/schema.d.ts`.

**Why it matters**

A 500 is 'the server broke', not 'this is not built'. Every call to an unbuilt endpoint is an unhandled exception counted as a 5xx by `ObservabilityMiddleware`, so a genuine server fault cannot be told apart from an unimplemented one in the operational metrics an operator watches. The endpoints are also offered to the dashboard as typed, callable API surface by the generated client. Nothing in `apps/web` calls them today, so nothing is user-visibly broken yet.

#### 18. GET /orders accepts a naive `since` and feeds it straight into a TIMESTAMPTZ comparison, while the sibling backtest endpoint rejects exactly that

`apps/api/src/atp_api/routers/orders.py:204` · Broken · 🟠 Medium · ✅ Verified · 🔴 **Open**

**Evidence**

> apps/api/src/atp_api/routers/orders.py:204 declares the query parameter with no timezone check and no `Annotated[..., Query(...)]` validator:
>
>     since: datetime | None = None,
>
> It is handed straight to the repository, which drops it into SQL against a `TIMESTAMPTZ` column — libs/core/src/atp_core/persistence/orders.py:197-198:
>
>         if since is not None:
>             query = query.where(OrderRow.created_at >= since)
>
> (`OrderRow.created_at` is `mapped_column(DateTime(timezone=True))` in libs/core/src/atp_core/persistence/models.py.)
>
> The sibling handler for the same kind of input does the opposite — apps/api/src/atp_api/routers/backtests.py:491-492:
>
>     if payload.start.tzinfo is None or payload.end.tzinfo is None:
>         raise _bad_request("start and end must be timezone-aware (CLAUDE.md §1.2)")
>
> and apps/api/src/atp_api/routers/analytics.py:276-277 takes `date` parameters and attaches the zone itself:
>
>         datetime.combine(resolved_start, time.min, tzinfo=UTC),
>         datetime.combine(resolved_end, time.max, tzinfo=UTC),

**Why it matters**

CLAUDE.md §1.2 says naive datetimes are rejected at the domain boundary. `GET /api/v1/orders?since=2026-08-27T00:00:00` — an offset-less ISO string, which is what `datetime.isoformat()` produces for a naive local datetime and what a hand-written curl or client will commonly send — parses to a naive `datetime` and reaches the driver unchecked. asyncpg normalises a naive value for a `timestamptz` parameter through `.astimezone()`, i.e. the API process's own local zone, so the same request string selects a different window depending on the container's TZ setting rather than on anything the caller or the API stated. The failure is silent: the order table simply returns the wrong slice of history, and the screen this endpoint exists to serve is the one where a missing refusal or fill is the thing being looked for. Every other datetime-shaped boundary in the API either rejects naive input or attaches UTC itself; this one does neither.

**Verification note**

Proven by execution. With a spy repository, `GET /api/v1/orders?since=2026-08-27T00:00:00` returns HTTP 200 and the repository receives a datetime with `tzinfo=None`. The same request with a `Z` or `+08:00` suffix arrives correctly zoned.

#### 19. `unsubscribe` from the last watched symbol turns the quote filter into a firehose

`apps/api/src/atp_api/ws.py:245` · Broken · 🟠 Medium · ⚠️ Reported · 🟢 **Closed** — @claude (#121)

*Record note (§10, 2026-09-02): Cited `:203` on 2026-08-27; the code is at `:245` today.*

**Evidence**

> `_wants` ends with:
> ```python
> watched = self._subscriptions.get(client_id, set())
> return not watched or symbol in watched
> ```
> and `unsubscribe` (ws.py:186) only does `self._subscriptions[client_id].difference_update(...)` — it never clears the channel membership. So removing the last symbol empties the set, and the empty set is the module's "everything on this channel" sentinel (comment at ws.py:198-201). Verified by running the real `ConnectionManager`:
>
>     m.subscribe("a", ["quotes"], ["AAPL"])
>     broadcast("quotes", {"symbol": "TSLA"})   -> not delivered
>     m.unsubscribe("a", ["AAPL"])
>     broadcast("quotes", {"symbol": "TSLA"})   -> DELIVERED
>
> The existing test `tests/unit/test_dashboard_ws.py:161 test_unsubscribing_stops_that_symbol_only` unsubscribes AAPL out of {AAPL, MSFT}, so it never reaches the empty-set case.

**Why it matters**

A client that sends `{"type":"unsubscribe","symbols":[...]}` for its last symbol — a dashboard panel unmounting, a watchlist emptied — is silently promoted from a filtered feed to every tick in the universe, which is the exact outcome the module docstring and `ConnectionManager`'s docstring say the per-subscription fan-out exists to prevent. The server pays the fan-out for every symbol on every tick and the browser pays the parse, and the operator's only signal is a tab that gets slow.

#### 20. A client dropped on the send deadline is never closed, so it is muted permanently and never reconnects

`apps/api/src/atp_api/ws.py:359` · Broken · 🟠 Medium · ⚠️ Reported · 🟢 **Closed** — @claude (#120)

*Record note (§10, 2026-09-02): Cited `:228` on 2026-08-27; the code is at `:359` today.*

**Evidence**

> `broadcast` drops a slow client with `self.disconnect(client_id)` (ws.py:227-228), and `disconnect` (ws.py:164-168) only pops the three dicts — it never calls `ws.close()`. The socket is therefore still open, and `websocket_endpoint`'s loop (ws.py:414-415) keeps awaiting `ws.receive_text()`. `subscribe` (ws.py:181-182) early-returns for an id not in `_connections`, while `_handle` still answers `await ws.send_json({"type": "subscribed"})` (ws.py:452) and still answers `pong` (ws.py:444).

**Why it matters**

A browser on a bad connection that misses one 2s `SEND_TIMEOUT_SECONDS` deadline is removed from the fan-out but its socket stays up. Its `onclose` never fires, so `useDashboardStream`'s reconnect ladder (apps/web/src/hooks/useLiveDashboard.ts:186-206) never runs; re-subscribing is acked with `{"type":"subscribed"}` and does nothing. The client sees a healthy socket, answers pings, and receives no quote, fill or halt again until the page is reloaded — including the halt broadcast that ws.py delivers unconditionally precisely because it must not be missed.

### apps/web (dashboard)

#### 21. The 204 test passes with the 204 branch deleted, so the logout path it guards is untested

`apps/web/src/api/client.test.ts:104` · Broken · 🟠 Medium · ⚠️ Reported · 🔴 **Open**

**Evidence**

> client.test.ts:104-107:
>     it('returns undefined for 204 rather than parsing an empty body', async () => {
>       mockFetch(204, undefined)
>       await expect(apiGet('/api/v1/x')).resolves.toBeUndefined()
>     })
>
> `mockFetch` (client.test.ts:13-22) builds `json: async () => body`, i.e. `async () => undefined` — it never models a real 204, whose body fails to parse. So `res.json()` and the guard return the same value.
>
> Verified by mutation: replacing client.ts:78 `return res.status === 204 ? (undefined as T) : res.json()` with `return res.json()` and running `npx vitest run src/api/client.test.ts` gives "10 passed (10)".

**Why it matters**

`/api/v1/auth/logout` returns 204 (auth.py:168, `status_code=status.HTTP_204_NO_CONTENT`) and `useLogout` calls it through `apiPost<void>`. If the 204 branch regressed, a real logout would reject on `res.json()`, `onSuccess` would never run, and the session cache and every other cached query would be left in place after signing out — with the whole suite still green.

#### 22. The two "reports a failure that never reached the API" web tests stub a valid JSON body, so they exercise the opposite branch of client.ts and assert nothing about it

`apps/web/src/components/killswitch.test.tsx:118` · Broken · 🟠 Medium · ⚠️ Reported · 🔴 **Open**

**Evidence**

> ```tsx
> 113  it('reports a failure that never reached the API', async () => {
> 114    // No JSON body at all — the dev server's proxy or nginx answering for an
> 115    // API it could not reach. `client.ts` turns that into a sentence about a
> 116    // machine; the button must render whatever it produced rather than an
> 117    // empty alert.
> 118    stubHalt({ status: 502 })
> ...
> 123    const alert = await screen.findByRole('alert')
> 124    expect(alert.textContent?.trim()).not.toBe('')
> ```
>
> But `stubHalt` (line 38) is `json: async () => route.body ?? {}` — it *always* resolves valid JSON. In apps/web/src/api/client.ts:68-74 the discriminator is whether `res.json()` threw:
>
> ```ts
> const body = await res.json().catch(() => null)
> const detail = typeof body?.detail === 'string' ? body.detail
>   : body !== null ? res.statusText          // <- this branch is taken
>   : unreachableDetail(res)                  // <- the branch the test names
> ```
>
> Verified empirically with a throwaway vitest that reuses the exact `stubHalt` helper against the real `apiPost`: the thrown error is `502: stub`. `unreachableDetail()` is never called, so nothing about "could not be reached" / `docker compose ps` / `/readyz` is tested here, and `not.toBe('')` passes for any non-empty alert including a plain `502: stub`.
>
> The identical defect is at apps/web/src/components/resume.test.tsx:179 (`stubResume({ status: 502 })`, same helper at line 53, same `expect(alert.textContent?.trim()).not.toBe('')` at line 186). The correct helper already exists one directory over — apps/web/src/api/client.test.ts:29 `mockProxyFailure` makes `json()` throw a SyntaxError — but it was not reused.

**Why it matters**

Both files claim to pin the behaviour of the emergency-stop and resume controls when nginx or the Vite proxy answers for an API it cannot reach — the one moment an operator is looking at this screen. Delete `unreachableDetail` from client.ts, or drop 502 from `PROXY_COULD_NOT_REACH_THE_API`, and these two tests still pass; only client.test.ts:68-85 would catch it, and it does not cover either button. The residual assertion (`textContent !== ''`) cannot distinguish the useful message from `502: Bad Gateway`, which is the regression the comment says was already shipped once.

#### 23. The audit screen can only filter 6 of the 11 actions the platform writes, and its docstring claims the missing handlers are stubs

`apps/web/src/pages/Audit.tsx:38` · Inconsistency · 🟠 Medium · ⚠️ Reported · 🔴 **Open**

*Record note (§10, 2026-09-02): Its own count has drifted: the platform now writes **12** audit verbs, not 11, so the screen filters 6 of 12.*

**Evidence**

> Audit.tsx:38-46 offers exactly: '', halt_engaged, login, login_failed, rate_limited, forbidden, logout. Audit.tsx:27-36 (`TONE`) tints the same six.
>
> libs/core/src/atp_core/audit/ports.py:83-158 defines eleven verbs, all documented as "Only actions that actually occur are listed": LOGIN, LOGIN_FAILED, LOGOUT, RATE_LIMITED, FORBIDDEN, HALT_ENGAGED, HALT_CLEARED, STRATEGY_CREATED, ORDER_CANCELLED, POSITION_CLOSED, FLATTEN_ALL.
>
> The writers are live: `rg NotImplementedError apps/api/src/atp_api/routers/risk.py` returns nothing, and risk.py:747/850/961/980 record Action.HALT_ENGAGED, Action.HALT_CLEARED and Action.FLATTEN_ALL.
>
> Audit.tsx:9-13 nevertheless states: "What is on the screen today is authentication and refusals ... the order-flow and kill-switch events land with their handlers, every one of which is still a stub (ADR 0010)." The file contradicts itself four lines later, where `halt_engaged` is called "the loudest thing this log records".

**Why it matters**

`flatten_all` is, by ADR 0005's own words, the one action that reaches a venue around the risk chain — and on the audit screen it cannot be selected in the filter and renders in the same neutral grey as a routine sign-in. `halt_cleared` is equally unreachable, so "who resumed trading, and when" — the deliberate act ADR 0009 puts behind a password re-prompt — has no filter on the screen built to answer it. An operator reviewing an incident with the filter set to "Trading halted" sees engagements only and concludes trading is still stopped.

### apps/worker

#### 24. Preflight's remedy for missing bar history is a command that cannot run — `--start` is required

`apps/worker/src/atp_worker/preflight.py:319` · Broken · 🟠 Medium · ⚠️ Reported · 🔴 **Open**

*Record note (§10, 2026-09-02): Cited `:311` on 2026-08-27; the code is at `:319` today.*

**Evidence**

> preflight.py:306-320 emits, for both the "no stored bars" and "short history" FAILs:
>
>     fix=f"uv run python scripts/backfill_bars.py --symbols {symbol} --verify"
>
> But scripts/backfill_bars.py:49 declares `p.add_argument("--start", required=True, help="YYYY-MM-DD")`. Running the suggested command verbatim:
>
>     $ uv run python scripts/backfill_bars.py --symbols SPY --verify
>     backfill_bars.py: error: the following arguments are required: --start
>
> (exit 2, before any configuration is loaded). Every other place in the repo that quotes this command supplies `--start` — docs/DATA.md:42, docs/FIRST_PAPER_RUN.md:77, docs/RUNBOOK.md:120, Makefile:152, and the script's own usage line at backfill_bars.py:4.

**Why it matters**

`check_warmup` is a FAIL, so it is the gate an operator hits when standing up a paper week (docs/FIRST_PAPER_RUN.md ranks it fourth on what breaks first). The one actionable string preflight hands them exits 2 with an argparse error, at the exact moment they are trying to get a strategy warmed up before the open.

#### 25. `queue.run`'s docstring claims the container waits for the in-flight backtest on SIGTERM, but no `stop_grace_period` is set so Docker kills it after 10s

`apps/worker/src/atp_worker/queue.py:237` · Inconsistency · 🟠 Medium · ⚠️ Reported · 🔴 **Open**

**Evidence**

> queue.py:236-237: "`Worker.run` installs its own signal handlers and finishes the job in flight before exiting, which is why the container sends SIGTERM and waits." The `queue` service in docker-compose.yml (lines 186-224) declares `build`, `env_file`, `environment`, `depends_on`, `volumes`, `command: python -m atp_worker.queue` and `restart: unless-stopped` — `grep -n stop_grace docker-compose.yml` returns nothing, so Docker's default 10-second grace period applies. `JOB_TIMEOUT_SECONDS = 3600` (queue.py:83) and the module header describes the work as "a multi-year minute-bar run [that] is minutes of solid Python".

**Why it matters**

On every `docker compose restart queue`, `make deploy`, or host reboot, a backtest more than 10 seconds from finishing is SIGKILLed rather than drained. The graceful-shutdown behaviour the comment relies on is never exercised, and each such stop manufactures exactly the orphaned `running` row that the startup sweep (above) cannot correct.

### docs

#### 26. README.md points requirement #5 (paper trading) at `brokers/paper.py`, a file that does not exist and that ADR 0003 deliberately rejected

`README.md:20` · Broken · 🟠 Medium · ✅ Verified · 🔴 **Open**

**Evidence**

> README.md:20: "| 5 | **Paper trading** on live data, no real money | `libs/core/.../brokers/paper.py` + Alpaca paper endpoint |"
>
> libs/core/src/atp_core/brokers/ contains only __init__.py, alpaca.py, ports.py, simulated.py. docs/ARCHITECTURE.md:16-22 and docs/ROADMAP.md:791-794 both state the design: "Paper and live are the same adapter on different hosts, which is the whole of requirement #5 at this layer — there is no `if paper:` anywhere in it." docs/adr/0003-alpaca-first-broker.md:18-20: "Paper trading is a **separate endpoint with the same API**, so requirement #5 is satisfied by configuration rather than by a simulator we would have to build and then trust."

**Why it matters**

The README's capability table is the first map a new contributor or agent reads, and it sends them looking for a per-mode broker adapter that ADR 0003 and ARCHITECTURE.md exist to argue against. CLAUDE.md §7 tells agents to prefer filling an existing stub over inventing a module; this entry invites exactly the invented module — a second Alpaca adapter — which would reintroduce the `if paper:` branch the architecture is built to avoid.

#### 27. README.md says switching to live requires "an explicit env flag plus a typed confirmation"; no typed confirmation exists anywhere on the live-mode path

`README.md:74` · Inconsistency · 🟠 Medium · ⚠️ Reported · 🔴 **Open**

**Evidence**

> README.md:72-74: "The default run mode is `paper`, and switching to `live` requires an explicit env flag plus a typed confirmation."
>
> libs/core/src/atp_core/config.py:232-239 `_guard_live_trading` checks only `ATP_ALLOW_LIVE_TRADING`; apps/worker/src/atp_worker/trading.py:118 adds `WORKER_ALLOW_LIVE_ORDERS`. All three controls are env flags. A repo-wide search for a typed confirmation finds it only on `POST /api/v1/risk/flatten-all` (docs/FIRST_PAPER_RUN.md:53, docs/adr/0005-single-execution-path.md:32, libs/core/src/atp_core/risk/killswitch.py:438) — a different act entirely. docs/SAFETY.md's layer table and CLAUDE.md §1.8 both describe the live guard as env flags only, with no confirmation step.

**Why it matters**

The README's Safety section is what a reader consults before deciding how carefully to handle live mode, and it promises an interactive human checkpoint that does not exist: a stray `-e ATP_RUN_MODE=live -e ATP_ALLOW_LIVE_TRADING=true` on a docker run is the whole of it. Believing there is a typed prompt in the way is precisely the belief that makes the two-flag guard feel like a formality.

#### 28. DASHBOARD.md says the audit trail records only authentication and refusals because the other handlers are stubs; six more verbs are wired

`docs/DASHBOARD.md:233` · Inconsistency · 🟠 Medium · ⚠️ Reported · 🔴 **Open**

*Record note (§10, 2026-09-02): Cited `:174` on 2026-08-27; the code is at `:233` today.*

**Evidence**

> docs/DASHBOARD.md:174-175: "What is recorded today is authentication and refusals. Order flow and kill-switch changes are not, because those handlers are stubs; see ADR 0010."
>
> libs/core/src/atp_core/audit/ports.py defines eleven verbs, six beyond auth/refusals: HALT_ENGAGED, HALT_CLEARED, STRATEGY_CREATED, ORDER_CANCELLED, POSITION_CLOSED, FLATTEN_ALL — each documented with the live handler that writes it (e.g. "One working order cancelled by a human, through `DELETE /orders/{id}` or as part of `POST /orders/cancel-all`"). docs/SAFETY.md:186-190 states the current truth: "It records signing in and out, failed attempts, lockouts, actions refused to a read-only session, and both halves of the kill switch — `halt_engaged` and `halt_cleared`." docs/DASHBOARD_STATUS.md:38-41 says the halt endpoint "writes an audit row (#70)". docs/adr/0010-rate-limiting-and-the-audit-trail.md is the historical record this line cites and has not been superseded, so the stale claim is being propagated forward from it.

**Why it matters**

The audit page is what an operator opens after an incident to answer "who stopped trading on Tuesday". Being told on the dashboard's own reference page that halts are not recorded means not looking for the `halt_engaged`/`halt_cleared` rows that are there — and, in the other direction, treating the absence of a row as expected rather than as the signal it is (the audit ports docstring is explicit that an absent halt row means "not halted *from the dashboard*").

#### 29. DASHBOARD_STATUS.md says `donchian_breakout` and `opening_range_breakout` "exist in code"; neither appears anywhere in the repository

`docs/DASHBOARD_STATUS.md:96` · Inconsistency · 🟠 Medium · ⚠️ Reported · 🔴 **Open**

**Evidence**

> docs/DASHBOARD_STATUS.md:95-97: "The stored rows against the registered classes, which is the point of the screen: `donchian_breakout` and `opening_range_breakout` exist in code and have never run."
>
> `rg -n "donchian|opening_range" --glob '!node_modules' .` returns only this doc line plus an unrelated web test fixture literally named 'donchian' (apps/web/src/pages/backtests.test.tsx:691). libs/core/src/atp_core/strategy/examples/ contains only buy_and_hold.py, sma_crossover.py and rsi_mean_reversion.py; the only `@register`ed classes are `name = "buy_and_hold"` (buy_and_hold.py:56) and `name = "sma_crossover"` (sma_crossover.py:32), and examples/__init__.py exports exactly those two plus the rule set.

**Why it matters**

This sentence is the doc's evidence for what the Strategies tab is for — the gap between registered classes and stored rows. With the two named classes absent, the registry side of that comparison holds only the two strategies that do run, so the screen's stated purpose is illustrated by strategies nobody can select. Anyone using this audit to decide what the picker should offer, or to check the tab against reality, is checking against invented rows.

#### 30. PARKING_LOT.md declares nothing is parked while ADR 0017 documents a diagnosed, deliberately deferred defect in shipped code

`docs/PARKING_LOT.md:34` · Inconsistency · 🟠 Medium · ⚠️ Reported · 🔴 **Open**

**Evidence**

> docs/PARKING_LOT.md:1-3 defines its scope as "Known defects in things that are **already built**, deliberately deferred"; :34 "## Nothing is parked"; :45-47 "An empty parking lot is a statement, not an absence: it says that nothing shipped is known to be wrong and deferred."
>
> docs/adr/0017-backtests-price-off-adjusted-closes.md:112-119: "**The live warmup has the same defect and is not fixed here.** `StrategyRunner` builds its indicator window from `b.close` on stored bars, so a live SMA(200) spanning a split is computed across the discontinuity for the length of its lookback… fixing it means deciding how a live loop holds two price spaces at once… and deserves its own ADR."
>
> Still present: apps/worker/src/atp_worker/runner.py:187 — `return np.array([float(b.close) for b in window], dtype=float)` — raw closes, no `adj_close` conversion. That is discovered, diagnosed, quantified (ADR 0017 records the same defect booking a 1:8 reverse split as +51.16% in one day on the backtest side), in a built subsystem, and explicitly deferred.

**Why it matters**

The parking lot is where CLAUDE.md's process routes exactly this class of item, and its own text makes the empty state a positive claim rather than a blank. A reader takes "nothing shipped is known to be wrong and deferred" at face value and never learns that a live strategy's indicators are computed across an unadjusted split for the length of its lookback — the failure mode the parking lot exists to stop people rediscovering cold.

#### 31. RISK_IMPLEMENTATION_NOTES.md states StrategyRunner and the trade-updates stream do not exist; both are built and call OrderRouter

`docs/RISK_IMPLEMENTATION_NOTES.md:15` · Inconsistency · 🟠 Medium · ⚠️ Reported · 🔴 **Open**

**Evidence**

> docs/RISK_IMPLEMENTATION_NOTES.md:15-16: "the risk chain now has a production caller in `OrderRouter`, and the runner that would drive it does not exist yet." :45-47: "nothing calls `OrderRouter` in production either, because `StrategyRunner` and the trade-updates stream are unstarted Phase 4 items."
>
> apps/worker/src/atp_worker/runner.py:223 `class StrategyRunner:` — it takes `router: OrderRouter` (runner.py:246) and calls `self.router.submit_signal(...)` (:875), `self.router.flatten(...)` (:531, :772) and `self.router.submit_protective_orders(...)` (:1143). libs/core/src/atp_core/execution/trade_updates.py exists with `apply_trade_update` (:59). apps/worker/src/atp_worker/trading.py:44 imports `StrategyRunner` and wires it. docs/ROADMAP.md:1129 lists `warmup`, `run`, `evaluate`, `on_fill_event` and `shutdown` as implemented, and :954 references `trading.consume_trade_updates`.

**Why it matters**

README.md:87 points implementers at this file as the live guide for "where RISK.md and the code disagree", and CLAUDE.md §7 tells agents to read the relevant docs page before implementing a subsystem. The file's central claim — that the risk chain has no production driver — is now false, so an agent following it would either rebuild a runner that exists or conclude the risk chain is unexercised when it is the runner's only path to a broker.

#### 32. RISK_IMPLEMENTATION_NOTES.md item 8 says `flatten_at_close` is never referenced and gives "silent no-op protection"; it is referenced and now raises

`docs/RISK_IMPLEMENTATION_NOTES.md:241` · Inconsistency · 🟠 Medium · ⚠️ Reported · 🔴 **Open**

**Evidence**

> docs/RISK_IMPLEMENTATION_NOTES.md:239-247: "### 8. `flatten_at_close` is a field nobody reads / `RiskSpec.flatten_at_close` (`strategy/rules.py:132`) exists and is never referenced anywhere else in the repo… A strategy author can set it today and get silent no-op protection… Either implement it with the session calendar or mark it explicitly unsupported until Phase 4." The file's summary at :13 lists item 8 as one of two still open.
>
> The second option was taken: libs/core/src/atp_core/strategy/rules.py:459-464 raises `InvalidRuleError` on any spec that sets it, pinned by tests/unit/test_rule_compilation.py:486. The field itself is now at rules.py:172, not :132. This file's stated convention (":41 'Each is annotated in place rather than deleted, so the reasoning survives'") means a resolved item gets a **RESOLVED** annotation; item 8 has none.

**Why it matters**

README.md:87 markets this file as the authority on where RISK.md and the code disagree, and its own header tells the reader to "Delete the file once the rest are closed". Item 8 is the reason the file is still alive, and it is closed — so the file overstates its own remaining work, and anyone acting on it would implement or refuse a behaviour that is already refused.

#### 33. ROADMAP's ticked Phase 2 item still states the backtest risk chain is empty and sizing is fixed-qty-only; both changed and BACKTESTING.md says so

`docs/ROADMAP.md:362` · Inconsistency · 🟠 Medium · ⚠️ Reported · 🟢 **Closed** — @claude (#129)

*Record note (§10, 2026-09-02): Cited `:337` on 2026-08-27 and re-pointed to `:362`, which is the corrected paragraph rather than the stale one — the caveat text this named is gone. Confirmed against `backtest/runner.py`, `risk/engine.backtest_rules` and docs/BACKTESTING.md before being fixed, but the evidence mark stays ⚠️ because §2 fixes that axis at the time of writing; the state axis is what moved. §10.5 has the account.*

**Evidence**

> docs/ROADMAP.md:334-339 (under the ticked `- [x] SmaCrossover runs end to end`): "Two caveats the CLI prints on every run… Sizing is a fixed share count (`--qty`)… And no pre-trade rule refuses anything: orders are routed through `RiskEngine`, but the chain is empty."
>
> libs/core/src/atp_core/backtest/runner.py:254-258 `build_engine(..., with_rules: bool = True)` builds `RiskEngine(limits, rules=backtest_rules() if with_rules else [])`, and its docstring says "Until now this built `RiskEngine(limits, rules=[])`, which refused nothing, and every result carried a warning saying so." runner.py:86-88 on FIXED_QTY_WARNING: "No longer said on *every* run — it is now a statement about a choice rather than about the platform." runner.py:90-92 on NO_RISK_RULES_WARNING: "Attached only to a run that deliberately asked for no rules. It used to be attached to all of them." scripts/run_backtest.py:97-128 exposes `--sizing`, `--sizing-value`, `--stop`, `--stop-value`. docs/BACKTESTING.md states the current behaviour: "**The rule chain is live in a backtest**, as `risk.engine.backtest_rules()` — five of the nine."

**Why it matters**

CLAUDE.md §6 makes ROADMAP.md "the only record of what this platform has and has not built" and says it is worthless the moment it lags the code. A ticked item's own body here describes a backtest that refuses nothing and sizes everything identically — the flattering configuration BACKTESTING.md warns about — so a reader auditing what a Phase 2 result is worth will discount runs that in fact carried the risk chain, and will not know that `--sizing risk_pct` exists.

#### 34. SAFETY.md's layered-defences table and go-live checklist omit `WORKER_ALLOW_LIVE_ORDERS`, the third lock the worker actually enforces

`docs/SAFETY.md:24` · Inconsistency · 🟠 Medium · ⚠️ Reported · 🟡 **Half-closed** — @claude (#124)

*Record note (§10, 2026-09-02): Re-pointed from `:23` to `:24`, the row #124 added. The table half of this finding is closed; the go-live checklist in the same file still does not list the lock, which is the open half. The finding also names `WORKER_ALLOW_LIVE_ORDERS`, an environment variable #124 removed — the lock now lives in `worker_config.allow_live_orders`.*

**Evidence**

> docs/SAFETY.md:20-27 lists layer 1 `ATP_RUN_MODE` and layer 2 `ATP_ALLOW_LIVE_TRADING` and no third flag; :98 "6. **Two independent locks stay two.**" `rg WORKER_ALLOW_LIVE_ORDERS docs/SAFETY.md` returns nothing.
>
> apps/worker/src/atp_worker/trading.py:118-124 refuses to place orders in live mode without it:
>     if settings.is_live and not settings.worker_allow_live_orders:
>         return TradingDecision(enabled=False, reason="live mode is enabled but WORKER_ALLOW_LIVE_ORDERS is false — this worker will not place real orders…")
> and trading.py:8-19 calls it "The three locks". libs/core/src/atp_core/config.py:122-129 calls it "The **third** lock". docs/DEPLOYMENT.md:280 lists it as "the worker's own lock" and docs/ROADMAP.md:1279 names all three.
>
> scripts/manage_secrets.py:26-28 mis-cites this page: "`ATP_RUN_MODE`, `ATP_ALLOW_LIVE_TRADING` and `WORKER_ALLOW_LIVE_ORDERS` are docs/SAFETY.md's layers 1 and 2" — three keys attributed to two layers that name only two of them.

**Why it matters**

SAFETY.md is the page CLAUDE.md §1.8 and README.md make mandatory reading before live trading, and its layer table is the canonical inventory of what must be true before real money moves. An operator who works the table sets two flags, deploys, and gets a worker that silently places no orders — and, worse in the other direction, a reviewer auditing the go-live controls counts two locks where the code has three, so the one lock specific to *unattended* trading is invisible to the checklist meant to enumerate them.

### infra / config / CI

#### 35. ATP_DB_PASSWORD is required by the deploy overlay and documented as a fill-in, but has no entry in .env.example

`.env.example:112` · Inconsistency · 🟠 Medium · ⚠️ Reported · 🟢 **Closed** — @claude (#113)

*Record note (§10, 2026-09-02): Cited `:67` on 2026-08-27; the code is at `:112` today.*

**Evidence**

> docker-compose.prod.yml:64 makes it mandatory and fail-closed:
>   POSTGRES_PASSWORD: ${ATP_DB_PASSWORD:?set ATP_DB_PASSWORD in .env - the base file default is the development password}
> and interpolates it into three DATABASE_URLs (lines 88, 99, 111).
>
> docs/DEPLOYMENT.md:186-192 tells the operator to start from the template and then fill it in:
>   cp .env.example .env
>   ...
>   Fill in, at minimum:
>   | `ATP_DB_PASSWORD` | **New.** The compose overlay refuses to start without it; the base file's password is `atp` |
>
> But .env.example contains no ATP_DB_PASSWORD line anywhere — the datastores section is only:
>   67: DATABASE_URL=postgresql+asyncpg://atp:atp@localhost:5432/atp
>   68: REDIS_URL=redis://localhost:6379/0
> (verified: grep -n ATP_DB_PASSWORD .env.example returns nothing; the only in-repo definitions are scripts/check_port_bindings.py:112's placeholder and scripts/manage_secrets.py:87's EXPECTED_KEYS).
>
> Every other read-elsewhere key IS documented in .env.example — ATP_WEB_BIND_ADDR (line 308) gets a 25-line block, ATP_DEV_PROXY_TARGET (line 282), VITE_* (275-276).

**Why it matters**

An operator following docs/DEPLOYMENT.md verbatim copies .env.example, works down the table, and finds no line to fill in for the first and only mandatory row. `make check-env` will not flag it either — check_env.py only reports keys present in .env that nothing reads, never keys that are absent. `make deploy` then aborts at compose parse time on a variable the template never mentioned, on a host, at deploy time.

#### 36. make check-tracked runs in no CI job, contradicting .gitignore's claim that it makes a swallowed SOPS bundle "fail the build"

`.github/workflows/ci.yml:163` · Inconsistency · 🟠 Medium · ✅ Verified · 🔴 **Open**

*Record note (§10, 2026-09-02): **Cites an absence.** Not a line — `ci.yml:163` is whatever step happens to sit there, and the finding is that no step runs `make check-tracked`. Still true at `4f68cf4`: CI calls `ruff`, `mypy` and `pytest` directly and never `make check`, which is the only target `check-tracked` hangs off.*

**Evidence**

> .gitignore:19-21 states the guarantee:
>   # `make check-tracked` covers this path so a future edit that
>   # swallows a bundle fails the build instead of producing a host with no
>   # credentials.
>
> Makefile:230 makes the same promise about scope:
>   check: check-tracked lint typecheck test  ## Everything CI runs — green before you push
>
> But ci.yml invokes exactly three make targets, and check-tracked is not among them:
>   line 163: run: make check-bindings
>   line 169: run: make up
>   line 451: run: make down
> The `python` job (lines 22-107) runs ruff/mypy/pytest directly and never calls `make check` or `make check-tracked`. (grep -n "check-tracked" .github/workflows/ci.yml returns nothing.)

**Why it matters**

check-tracked is the only thing standing between an over-broad .gitignore rule and files that never reach the repository — the failure that already cost this repo its entire market-data package. It runs only if a contributor remembers `make check` locally. For the paths with no other backstop — 'infra/**/*.sql', 'docs/**/*.md' and especially 'infra/env/*.sops.env' (Makefile lines 210-214) — nothing else in CI would notice: a .gitignore edit that swallows the SOPS bundle passes every CI job green and produces a deployment host with no credentials, which is precisely the outcome .gitignore:19-21 says is prevented.

#### 37. pre-commit pins ruff v0.5.5 while the lockfile and CI run ruff 0.16.3, and the hooks are never installed by any documented step

`.pre-commit-config.yaml:3` · Inconsistency · 🟠 Medium · ⚠️ Reported · 🔴 **Open**

**Evidence**

> .pre-commit-config.yaml:2-7
>   - repo: https://github.com/astral-sh/ruff-pre-commit
>     rev: v0.5.5
>     hooks:
>       - id: ruff
>         args: [--fix]
>       - id: ruff-format
>
> uv.lock:1806-1807 (what `make lint` and CI's Lint step actually execute):
>   name = "ruff"
>   version = "0.16.3"
> (.venv/bin/ruff --version confirms 0.16.3.)
>
> That gap spans ruff's rule rename: on 0.16.3, `ruff rule TCH003` now errors with "invalid value 'TCH003'
> tip: a similar value exists: 'TC003'", and pyproject.toml already uses the new spelling in one place (`"apps/api/**" = ["TC"]`) while `select` still carries the old one (`"TCH"`).
>
> Separately, nothing installs the hooks: `grep -rn "pre-commit install"` over the repo (excluding .venv/node_modules/.git) returns nothing. Makefile `install` (lines 15-17) runs only `uv sync --all-packages` and `npm install`; CONTRIBUTING.md's Setup block (lines 5-10) and CLAUDE.md §3 never mention it.

**Why it matters**

The gitleaks hook is labelled "Last line of defence before a broker key reaches a remote" (.pre-commit-config.yaml:21), but with no `pre-commit install` in `make install` or CONTRIBUTING.md, it never runs for anyone following the documented setup — the only gitleaks that runs is the CI job, which fires after the key has already reached the remote, the one failure CLAUDE.md §1.6 says cannot be fixed by a follow-up commit. And for anyone who does install the hooks, ruff-format at 0.5.5 formats to a style that `ruff format --check` at 0.16.3 rejects, so committing cleanly through the hooks produces a red `make lint` and a red CI Lint step.

#### 38. docker-compose and the Makefile still tell operators the worker cannot place orders, three locks after it can

`docker-compose.yml:204` · Inconsistency · 🟠 Medium · ⚠️ Reported · 🟢 **Closed** — @claude (#124)

*Record note (§10, 2026-09-02): Cited `:160` on 2026-08-27; the code is at `:204` today.*

**Evidence**

> docker-compose.yml:160-161: "# It does not trade. `StrategyRunner` is still a stub, so this ingests market # data and runs scheduled jobs; give it a watchlist with WORKER_SYMBOLS." and Makefile:31: `@echo "         it places no orders yet; set WORKER_SYMBOLS to give it a watchlist"`. Both are false: `apps/worker/src/atp_worker/runner.py` is a complete 1236-line `StrategyRunner` with `submit`, `_protect` and `on_fill_event`, and `main.py:186-218` wires it whenever `trading.decide(...)` returns `enabled` — which requires `WORKER_STRATEGY`, a non-empty watchlist and broker credentials, but for paper mode nothing more (`trading.py:118` gates only live on `worker_allow_live_orders`). `main.py`'s own module docstring gets it right: "**All three can run now** ... `WORKER_STRATEGY` names the strategy to trade".

**Why it matters**

These two lines are what an operator reads when bringing the stack up. Someone who believes the worker "does not trade" and sets `WORKER_SYMBOLS` plus `WORKER_STRATEGY` on a paper-credentialled `.env` gets an unattended loop submitting orders against a real Alpaca paper account — the opposite of what the comment promised, and the exact "a worker that starts trading because it was deployed rather than because somebody chose to" accident `trading.py`'s lock 1 is written to prevent.

### libs/core — analytics

#### 39. `comparability_warnings` tells every live-vs-backtest reader the backtest used flat share sizing, which stopped being true when backtests moved onto `position_size`

`libs/core/src/atp_core/analytics/performance.py:611` · Inconsistency · 🟠 Medium · ⚠️ Reported · 🔴 **Open**

**Evidence**

> Appended unconditionally to every comparison (performance.py:610-615):
>
>     notes.append(
>         "the backtest sized every entry at a flat share count and live sizing is "
>         "risk-based (docs/RISK.md 'Position sizing'), so the money-denominated "
>         "metrics — expectancy, the win and loss sizes, turnover, total return — "
>         "are partly a difference between two sizing rules"
>     )
>
> But `build_engine` wires `position_sizer=RiskBasedSizer(method, value)` for every run (runner.py:314), with `method` from `resolve_sizing`, which accepts `risk_pct`, `equity_pct`, `fixed_notional`, `volatility_target` (runner.py:101-103, 187-203). docs/BACKTESTING.md says so explicitly: "Sizing goes through `risk.rules.position_size` — the same function the live router calls, with the same arguments... Both used to be caveats here and neither is any more." `runner.FIXED_QTY_WARNING` was made conditional for exactly this reason (runner.py:80-88, 343-344: "No longer said on *every* run").

**Why it matters**

A `risk_pct` backtest compared against a `risk_pct` live run is annotated with a warning saying its money-denominated divergences are 'partly a difference between two sizing rules' when the two sizing rules are identical. The endpoint's own docstrings argue these notes exist so a reader does not act on an artefact; here the note manufactures one, and it is the last note in the list, read by the person deciding whether to keep a strategy running with real money.

### libs/core — backtest

#### 40. `turnover` is computed against starting equity in the backtest and against mean equity in analytics, then the two are subtracted as a divergence

`libs/core/src/atp_core/backtest/engine.py:724` · Inconsistency · 🟠 Medium · ⚠️ Reported · 🔴 **Open**

**Evidence**

> Engine (engine.py:724), denominator is starting equity and the numerator is every fill's notional including still-open positions:
>
>     turnover=float(self._traded_notional / starting) if starting else 0.0,
>
> Analytics (performance.py:849), denominator is the mean of the equity curve and the numerator counts only completed round trips:
>
>     traded = sum(float(t.entry_price * t.qty) + float((t.exit_price or Decimal(0)) * t.qty) for t in trades)
>     return traded / (sum(equities) / len(equities))
>
> Both land in the same `PerformanceMetrics.turnover` field, and `compare_to_backtest` subtracts every field of it (performance.py:514-518), including `turnover` — which `METRIC_BASIS` labels `BASIS_WINDOW` (metrics.py:132), i.e. comparable when the windows match.

**Why it matters**

`GET /analytics/live-vs-backtest/{run_id}` reports `live.turnover - backtest.turnover` as a fact about how much more or less the strategy traded. For a run that grew, mean equity exceeds starting equity, so the same trading activity yields a systematically smaller analytics turnover than engine turnover — a divergence produced by the two denominators, not by the strategy. None of `comparability_warnings`' notes names it, and the metrics module opens by claiming 'One implementation for both, so a paper-trading Sharpe is directly comparable to the backtested one'.

#### 41. `periods_per_year_for` floor-divides the session, so a 4h backtest is annualised at 252 — the daily basis

`libs/core/src/atp_core/backtest/metrics.py:60` · Broken · 🟠 Medium · ⚠️ Reported · 🔴 **Open**

**Evidence**

>     SESSION_SECONDS = 390 * 60          # metrics.py:42
>     ...
>     if timeframe is Timeframe.D1:
>         return TRADING_DAYS_PER_YEAR
>     return TRADING_DAYS_PER_YEAR * (SESSION_SECONDS // timeframe.seconds)   # metrics.py:60
>
> Actual output for every supported timeframe:
>
>     1m 98280   (bars/session 390.0)
>     5m 19656   (78.0)
>     15m 6552   (26.0)
>     30m 3276   (13.0)
>     1h 1512    (6.5   → should be 1638)
>     4h 252     (1.625 → should be ~410)
>     1d 252
>
> 23400 // 14400 == 1, so `4h` returns the same 252 as `1d`; 23400 // 3600 == 6, so `1h` loses the half-hour. `Timeframe.H4` and `H1` are both accepted by `build_engine` (runner.py:282) and by `--timeframe` (scripts/run_backtest.py:242).

**Why it matters**

This value feeds `compute_all` for CAGR (`years = len(returns) / periods_per_year`), Sharpe, Sortino, Calmar and volatility (metrics.py:310-335). A one-year 4h backtest has ~410 bars, so it reports `years ≈ 1.63` and understates CAGR accordingly, while Sharpe and annualised volatility are scaled by √252 instead of √410 — about 27% low. It is exactly the failure the function's own docstring warns about ('annualising it at 252 would understate its volatility by about that factor'), and it silently shifts the `sharpe > 3` suspicion threshold that `runner.suspicious` and the CLI apply.

### libs/core — config

#### 42. `RiskLimits` applies no bounds to any of the ceilings the whole platform's safety rests on

`libs/core/src/atp_core/config.py:29` · Broken · 🟠 Medium · ⚠️ Reported · 🔴 **Open**

**Evidence**

>     class RiskLimits(BaseSettings):
>         """Account-wide hard ceilings. … These are the last line of defence before a bug becomes a loss."""
>         model_config = SettingsConfigDict(env_prefix="RISK_", env_file=".env", extra="ignore")
>
>         max_position_pct: Decimal = Decimal("0.10")
>         max_gross_exposure_pct: Decimal = Decimal("1.00")
>         max_daily_loss_pct: Decimal = Decimal("0.03")
>         max_orders_per_minute: int = 30
>         max_open_positions: int = 20
>         max_quote_age_seconds: int = 30
>
> Every field is a bare annotation — no `Field(gt=0, le=...)`, no `field_validator`, and the only `model_validator` in the module is `Settings._guard_live_trading` (config.py:230), which does not touch `risk`. `config_problems()` (config.py:443-446) calls `RiskLimits()` and collects `ValidationError`s, but with no constraints declared, any Decimal-parseable value passes.
>
> The fields flow straight into comparisons with no further checking: `rules.py:133` `ceiling = limits.max_position_pct * equity`; `rules.py:251` `if len(self._recent) >= limits.max_orders_per_minute`; `rules.py:221` `if change <= -limits.max_daily_loss_pct`.
>
> This is the same defect docs/RISK_IMPLEMENTATION_NOTES.md item 6 raised against the sibling `PositionSizeSpec.value` and resolved ("the mistake worth catching at config time is a misplaced decimal point"), and the same class of failure config.py:472-478 documents at length for a *misspelled* key ("five times looser than the operator believes they just set"). The correctly-spelled-but-wrong-magnitude case is unguarded.

**Why it matters**

`.env.example:71` reads `RISK_MAX_POSITION_PCT=0.10          # max 10% of equity in one symbol` — the comment invites an operator to write `10`. `RISK_MAX_POSITION_PCT=10` loads cleanly and sets the single-position cap to 1000% of equity, i.e. `MaxPositionSizeRule` refuses nothing; the same typo on `RISK_MAX_GROSS_EXPOSURE_PCT` removes the leverage ceiling. In the other direction `RISK_MAX_ORDERS_PER_MINUTE=0` makes `len(self._recent) >= 0` true on the first order and denies every order forever, and a negated `RISK_MAX_DAILY_LOSS_PCT=-0.03` turns `change <= -(-0.03)` into a rule that blocks entries on any day not up more than 3%. None of these raises, logs, or appears in `config_problems()`, so the deployment reports itself healthy with its last line of defence removed.

### libs/core — data

#### 43. ALPACA_DATA_FEED does not control the WebSocket feed, and the stream logs a feed it is not connected to

`libs/core/src/atp_core/data/providers/alpaca.py:722` · Inconsistency · 🟠 Medium · ⚠️ Reported · 🔴 **Open**

**Evidence**

> The socket is opened from one setting and labelled from another:
>
>     alpaca.py:704   connection = await self._connect(self._settings.alpaca_stream_url)
>     alpaca.py:720-724   log.info(
>                             "data.alpaca.stream_connected",
>                             feed=self._settings.alpaca_data_feed,
>                             ...
>
> `alpaca_data_feed` is only ever sent on the REST path (`alpaca.py:308` in `get_bars`, `alpaca.py:407` in `get_latest_bar`). The stream's feed is baked into the URL's path:
>
>     config.py:64   alpaca_stream_url: str = "wss://stream.data.alpaca.markets/v2/iex"
>     config.py:65   alpaca_data_feed: Literal["iex", "sip"] = "iex"
>
> Nothing validates that the two agree (`config.py:230` `_guard_live_trading` is the only model validator, and it checks the live-trading locks only). Meanwhile docs/DATA.md:12 presents them as one feed — the Historical/Real-time table's Feed row reads "IEX (free) / SIP (paid)" then "same" — and .env.example:63-64 documents `ALPACA_DATA_FEED` as the switch: "# iex = free real-time. sip = full consolidated tape, paid subscription."

**Why it matters**

An operator who buys the SIP subscription and sets `ALPACA_DATA_FEED=sip` gets SIP bars from the backfill and IEX ticks (2–3% of consolidated volume) from the live stream, written into the same `bars` table and the same quote cache — and the startup line they would check, `data.alpaca.stream_connected feed=sip`, tells them it worked. Nothing in the process disagrees with them until fills stop matching the prices the strategy saw.

#### 44. StalenessMonitor re-arms itself at the closing bell and logs "market data is flowing again" while the feed is still dead

`libs/core/src/atp_core/data/stream.py:533` · Broken · 🟠 Medium · ⚠️ Reported · 🔴 **Open**

*Record note (§10, 2026-09-02): Cited `:528` on 2026-08-27; the code is at `:533` today.*

**Evidence**

> stream.py:519-533:
>
>     if verdict.stale and not self._alerted:
>         self._alerted = True
>         ... self._halt(verdict)
>     elif not verdict.stale and self._alerted:
>         self._alerted = False
>         log.warning(
>             "data.staleness.recovered",
>             msg="market data is flowing again — the halt it engaged is still engaged",
>         )
>
> `verdict.stale` is False for two completely different reasons. `evaluate` returns `StalenessVerdict(stale=False, silent_for_seconds=None, market_open=False, reason="market is shut — silence is expected")` at stream.py:470-476 whenever the session is over. The re-arm branch does not test `verdict.market_open`, so a dead feed that is still dead at 16:00 takes the `elif`.
>
> This contradicts the contract stated two dozen lines above it, at stream.py:504-507: "Halts once per outage and never clears ... **When data resumes** this re-arms so the *next* outage is reported too" — and docs/DATA.md:185 ("It halts once per outage"). tests/unit/test_staleness_monitor.py::TestWatch has no case where an outage spans the close; `test_recovery_re_arms_but_never_clears_the_halt` (line 298) only exercises genuine recovery.

**Why it matters**

A feed that dies at 15:00 Tuesday and stays dead produces, at 16:00 that day, a WARNING telling the on-call operator that market data is flowing again — during the incident, in the log they are reading to decide whether the halt can be cleared. It then halts a second time the next morning for the same, single, unbroken outage, so "halts once per outage" is not what the code does either.

### libs/core — execution

#### 45. `submit_protective_orders` states a "load-bearing" caller contract that no production caller implements, leaving two public router methods dead outside tests

`libs/core/src/atp_core/execution/router.py:397` · Redundancy · 🟠 Medium · ✅ Verified · 🔴 **Open**

*Record note (§10, 2026-09-02): Cited `:387` on 2026-08-27; the code is at `:397` today.*

**Evidence**

> router.py:381-391 declares: "Two contracts this places on whoever calls it, **both load-bearing**: ... 2. Close from an engine-side trigger only over `abs(position.qty) - broker_side_protected_qty(symbol, position)`. The venue's stop fires for the rest; closing that part again would open a reversed position with nothing protecting it."
>
> The two accessors written to serve that contract — `broker_side_protected_qty` (router.py:541) and `has_broker_side_protection` (router.py:558) — have no caller in `apps/` or `libs/` at all; `rg 'broker_side_protected_qty|has_broker_side_protection'` returns only their definitions and tests/unit/test_order_router.py:1064, 1120, 1184.
>
> The sole production caller of `submit_protective_orders` is `StrategyRunner._protect` (apps/worker/src/atp_worker/runner.py:1143), and the engine-side trigger it pairs with is `StrategyRunner._check_stops` (runner.py:772), which closes the whole position:
>
>     result = await self.router.flatten(position.symbol, portfolio, purpose=reason)
>
> `flatten` builds `qty=abs(position.qty)` (router.py:730) — the full holding, with no subtraction of what the venue's stop already covers. Contract 1 of the pair *is* honoured (runner.py:1128 `_apply_to_portfolio` runs before runner.py:1136 `_protect`); contract 2 is not honoured anywhere.

**Why it matters**

With `stop_config.broker_side` true, a take-profit trigger (runner.py:821-825) sends a market close for 100% of a position whose venue-side stop is still working for the same 100%. `flatten` only cancels that stop *after* the close is acknowledged (router.py:738-740), so if the stop fires in the interval the position sells twice and the second sale opens a reversed, unprotected position — the exact outcome contract 2 was written to prevent by closing only the uncovered remainder. `flatten`'s own docstring (router.py:696-703) concedes this window and says the fix is a venue-side bracket, which leaves the stated contract and the two methods supporting it as documentation and API that describe behaviour the platform does not have.

**Verification note**

Confirmed. `broker_side_protected_qty` and `has_broker_side_protection` have no caller in `libs/` or `apps/` — only three assertions in `test_order_router.py`. `flatten` sizes at `abs(position.qty)` (router.py:730), and `runner.py:820-827` can still fire a take-profit exit while `broker_side` is on.

#### 46. `OrderRouter.flatten` says four risk rules can refuse an exit; ADR 0005, the API and the runner all say six — and six is correct

`libs/core/src/atp_core/execution/router.py:700` · Inconsistency · 🟠 Medium · ✅ Verified · 🔴 **Open**

*Record note (§10, 2026-09-02): Cited `:683` on 2026-08-27; the code is at `:700` today.*

**Evidence**

> router.py:682-684: "Exits bypass entry-blocking risk rules (e.g. the daily loss limit) but still pass through `validate()` ... **Four** rules can still refuse one, the kill switch among them".
>
> Every other statement of the same fact says six:
> - docs/adr/0005-single-execution-path.md:34 — "Six of the nine default rules can refuse an exit. Four judge the order rather than whether it reduces a position — the kill switch, trading hours, the rate limit and stale data — and two more refuse whenever any holding is unmarked."
> - apps/api/src/atp_api/routers/positions.py:183 — "Six of the nine rules can refuse an exit".
> - apps/worker/src/atp_worker/runner.py:741 — "Six of the nine default rules can refuse an exit".
> - The same file, 545 lines earlier: router.py:138-143 enumerates exactly six for a protective stop (itself a reducing order) and names the three that can never refuse one.
>
> The code agrees with six. For a full exit: `KillSwitchRule`, `TradingHoursRule`, `RateLimitRule`, `StaleDataRule` judge the order regardless of direction; `MaxPositionSizeRule` (risk/rules.py:118-120) and `MaxExposureRule` (risk/rules.py:154-156) both open with `if (denial := _unpriced_book(self.name, portfolio)) is not None: return denial` and both deny on `price is None`, so they refuse an exit whenever any other holding is unmarked. Only `DailyLossLimitRule` (rules.py:206), `BuyingPowerRule` (rules.py:306) and `MaxOpenPositionsRule` (rules.py:277-279) always allow one.

**Why it matters**

`flatten` is the emergency close path — an operator's button and the runner's stop/target/time exits all land here. Someone diagnosing a refused flatten reads this docstring to know which rules to suspect and is told that two of the six that can actually block it cannot: an unmarked, unrelated holding elsewhere in the book silently denies the close through `max_position_size` or `max_gross_exposure`, and this docstring rules that out. It also directly contradicts the ADR the same docstring points at eight lines below.

**Verification note**

Confirmed. `rg` finds ‘Six of the nine’ at `runner.py:741`, `positions.py:183` and `docs/adr/0005-single-execution-path.md:34`. `router.py:683` is the only place that says four.

### libs/core — persistence

#### 47. Four ORM relationship() declarations in models.py are used nowhere and would raise MissingGreenlet if they ever were

`libs/core/src/atp_core/persistence/models.py:227` · Redundancy · 🟠 Medium · ⚠️ Reported · 🔴 **Open**

*Record note (§10, 2026-09-02): Cited `:225` on 2026-08-27; the code is at `:227` today.*

**Evidence**

> models.py declares four relationships:
>   line 122 (StrategyRow): `orders: Mapped[list[OrderRow]] = relationship(back_populates="strategy")`
>   line 224 (OrderRow):    `strategy: Mapped[StrategyRow | None] = relationship(back_populates="orders")`
>   line 225 (OrderRow):    `fills: Mapped[list[FillRow]] = relationship(back_populates="order")`
>   line 239 (FillRow):     `order: Mapped[OrderRow] = relationship(back_populates="fills")`
>
> `grep -rn "back_populates|selectinload|joinedload|subqueryload|lazy=" --include=*.py .` (excluding .venv/__pycache__) returns exactly those four lines and nothing else. No code anywhere reads `row.fills`, `row.orders`, `row.strategy` or `row.order`.
>
> The job they would do is already done by hand, in a second mechanism: persistence/orders.py:200 `_fills_for(session, order_ids)` issues one `select(FillRow).where(FillRow.order_id.in_(order_ids))` and groups in Python, precisely to avoid the per-order lazy load `OrderRow.fills` would produce.
>
> And they cannot safely be used: every read in orders.py returns rows *after* the session closes — e.g. orders.py:118-124 `async with session_scope(...) as session: rows = ...` then `return [self._to_order(row, ...) for row in rows]` outside the block. `relationship()` defaults to `lazy="select"`, so touching `row.fills` on a detached row under an AsyncSession raises `MissingGreenlet`/`DetachedInstanceError`.

**Why it matters**

Dead declarations that read as a supported access path. A maintainer who writes `row.fills` instead of calling `_fills_for` gets no type error and no test failure — mypy accepts it because the attribute is declared `Mapped[list[FillRow]]` — and the code blows up at runtime with MissingGreenlet the first time it runs against a real async session, i.e. in the API or worker rather than in unit tests. If it is instead touched inside the session block, it silently becomes the N+1 that `_fills_for` exists to prevent.

### libs/core — risk

#### 48. `RedisKillSwitch.engage` is GET-then-SET, so its documented idempotence and alert deduplication break under concurrent halts

`libs/core/src/atp_core/risk/killswitch.py:214` · Broken · 🟠 Medium · ⚠️ Reported · 🔴 **Open**

**Evidence**

>         key = self._key(scope, target)
>         existing = _sync(self._client.get(key))
>         if existing is not None:
>             return _decode(existing)
>
>         record = HaltRecord(..., engaged_at=datetime.now(UTC), engaged_by=engaged_by, ...)
>         self._client.set(key, _encode(record))          # killswitch.py:226 — no nx=True
>         …
>         metrics.halt_engaged(scope, reason)
>         self._announce("engaged", record)
>         self._alert_engaged(record)
>
> The read and the write are two round trips with no `nx=True` and no Lua, so two processes that both miss on the GET both write. The method's own docstring (killswitch.py:204-207) promises otherwise: "An existing halt is returned unchanged rather than overwritten, so the record keeps who stopped trading and when it first stopped. A second engagement is not new information, and letting it reset the timestamp would erase the only audit trail of the original." ADR 0012 builds on the same guarantee — "Deduplication is the Redis state, not a flag. `engage` returns early when a halt is already recorded, so the alert is only reached by a *new* halt" — and `metrics.halt_engaged`'s docstring (registry.py:255) says "Only a halt that actually changed the state is counted". Concurrent engagers exist and are not hypothetical: `data/stream.py:382` and `:543` (the stream consumer process), `execution/router.py:1097` and `execution/reconciliation.py:145,:191` (the worker), and `routers/risk.py:701` (the API), all writing the same `atp:halt:global` key.

**Why it matters**

The realistic trigger is the ordinary incident shape: the feed drops and `StalenessMonitor` engages `DATA_FEED_LOST` in the worker at the same moment the operator hits HALT in the API. Both GET miss, both SET, and the loser's record is silently overwritten — the incident's first `engaged_at`, `engaged_by` and `reason` are gone, which is the one thing killswitch.py says the early return exists to preserve. Two CRITICAL phone alerts and two `halts_engaged` increments are emitted for one halt, and `/risk/halt`'s `already_halted_by_another` audit field (risk.py:756) reports False for a request that did not stop trading. A single `set(key, ..., nx=True)` re-read on failure would restore the guarantee.

#### 49. RedisKillSwitch stamps the halt record from the wall clock inside libs/core, where every sibling adapter takes an injected Clock for exactly this reason

`libs/core/src/atp_core/risk/killswitch.py:221` · Inconsistency · 🟠 Medium · ✅ Verified · 🔴 **Open**

**Evidence**

> libs/core/src/atp_core/risk/killswitch.py:218-225 — `RedisKillSwitch.engage` builds the audit record from the process wall clock, and the class takes no `Clock` at all (`__init__(self, client, key_prefix=..., *, alerts=None)`, line ~145):
>
>         record = HaltRecord(
>             scope=scope,
>             reason=reason,
>             engaged_at=datetime.now(UTC),
>             engaged_by=engaged_by,
>             detail=detail,
>             target=target,
>         )
>
> CLAUDE.md §1.2: "Never `datetime.now()` — use `atp_core.clock.Clock.now()`."
>
> Every comparable adapter in the same layer does the opposite and says why. libs/core/src/atp_core/persistence/strategies.py:132-138:
>
>     def __init__(self, session_factory: async_sessionmaker[AsyncSession], clock: Clock) -> None:
>         self._session_factory = session_factory
>         # Injected rather than `datetime.now()` (rule §1.2), and it matters more
>         # here than it looks: ...
>         self._clock = clock
>
> libs/core/src/atp_core/persistence/jobs.py:190-201 (`progress_for`): "Here rather than at the call site so that nothing constructing one reads the wall clock itself (CLAUDE.md §1.2)". libs/core/src/atp_core/execution/reconciliation.py:96-102: "Takes a `Clock` rather than reading the wall clock, so `checked_at` means the same thing in a backtest as in production (rule §1.2). It is required rather than defaulted..."

**Why it matters**

`engaged_at` is the only record of when trading stopped — the module docstring's own justification for returning an existing halt unchanged is that "letting it reset the timestamp would erase the only audit trail of the original". Reading it from the wall clock makes that timestamp the one value in the halt record that no caller controls and no test can freeze, and it means a halt engaged from a process running on a simulated or offset clock (a replay, a paper run driven off historical time, `POST /risk/halt` served by an API whose `get_clock` dependency exists precisely to be swapped) is stamped with machine time while every other record written in the same act — the reconciliation report, the audit entry at apps/api/src/atp_api/routers/positions.py, the strategy row — is stamped with the injected clock. Two records of one event carrying two different notions of "now" is the class of bug CLAUDE.md §5 names as the hardest here to notice. It is also the only `datetime.now()` in `libs/core` outside `SystemClock` itself and the two live venue adapters, and unlike those it carries no comment claiming a carve-out.

**Verification note**

Confirmed. This is the only `datetime.now()` in `libs/core` outside `SystemClock` itself (clock.py:40) and the two live venue adapters (`brokers/alpaca.py`, `data/providers/alpaca.py`). `RedisKillSwitch.__init__` takes no `Clock` at all.

### scripts

#### 50. check_deployed_shape only guards api and worker, so the queue service can silently deploy host source

`scripts/check_port_bindings.py:108` · Broken · 🟠 Medium · ⚠️ Reported · 🔴 **Open**

*Record note (§10, 2026-09-02): Cited `:101` on 2026-08-27; the code is at `:108` today.*

**Evidence**

> scripts/check_port_bindings.py:98-101
>   #: Services whose code must come from the image rather than from the host. The
>   #: database is deliberately not one of them: its bind mount is `infra/db/init`,
>   #: which is configuration read once at initdb, not code.
>   CODE_SERVICES = ("api", "worker")
>
> But three services bind-mount source in docker-compose.yml, not two:
>   api    (lines 79-81)  ./libs:/app/libs, ./apps/api:/app/apps/api
>   worker (lines 181-183) ./libs:/app/libs, ./apps/worker:/app/apps/worker
>   queue  (lines 213-215) ./libs:/app/libs, ./apps/worker:/app/apps/worker
>
> and docker-compose.prod.yml resets all three, queue included:
>   line 86  api:    volumes: !reset []
>   line 97  worker: volumes: !reset []
>   line 109 queue:  volumes: !reset []   # comment: "Same three corrections as `worker` above and for the same reasons: no source bind-mounted over the image"
>
> check_deployed_shape() iterates `for name in CODE_SERVICES` (line 228) and never looks at `queue`; on success it prints "deployed shape: code comes from the image" (line 250). The CODE_SERVICES comment justifies excluding `db` and says nothing about `queue`.

**Why it matters**

`make deploy` runs check-bindings as a gate (Makefile line 74). If a future edit drops or breaks `volumes: !reset []` for `queue` alone in docker-compose.prod.yml — the "an overlay someone edits later" case the module docstring at lines 46-53 names explicitly — the check prints "deployed shape: code comes from the image", exits 0, and the deploy proceeds with the queue container running ./libs and ./apps/worker from the host checkout instead of the built image. That is the process that executes every queued backtest, and it is the exact failure the check exists to prevent, reported as passing.

#### 51. `scripts/halt.py` tells an operator that `/risk/resume` is a stub and that flattening has no operator path; both are implemented

`scripts/halt.py:15` · Inconsistency · 🟠 Medium · ⚠️ Reported · 🔴 **Open**

**Evidence**

> scripts/halt.py module docstring:
>
>     **Clearing is still only here.** `POST /api/v1/risk/resume` remains a stub and
>     demands a step-up password no screen asks for yet, so `clear --by <name>` is the
>     sole path back to trading.
>     …
>     Flattening realises P&L and is a separate decision with no operator path yet —
>     use the broker's own UI (docs/RUNBOOK.md 'Emergency flatten').
>
> Both claims are false. `apps/api/src/atp_api/routers/risk.py:775` defines `@router.post("/resume") async def clear_kill_switch(...)` with a full implementation (step-up password via `_require_step_up`, `kill_switch.clear` off the event loop, audit row, `ResumedView`), and `risk.py:879` defines `@router.post("/flatten-all")`. The dashboard does ask for the password — `apps/web/src/components/ResumeButton.tsx` exists, with `apps/web/src/components/resume.test.tsx` beside it. docs/SAFETY.md:151-156 states the opposite of this file: "`/risk/resume` and `/risk/flatten-all` additionally require the account password in the request body." killswitch.py:434-441 also documents `POST /api/v1/risk/flatten-all` as the shipped act.

**Why it matters**

This file is what an operator reads during an incident — its own header calls it "docs/SAFETY.md's layer 6 with a handle on it". It sends them to the broker's UI to flatten and tells them the dashboard cannot resume, at the moment when using the platform's own audited path (which writes `Action.HALT_CLEARED` / the flatten audit row) is what you want. Acting on the stale text means the halt is cleared or the book is liquidated with no audit trail.

#### 52. `halt.py status` accepts and validates `--scope`/`--target` and then ignores them

`scripts/halt.py:78` · Broken · 🟠 Medium · ⚠️ Reported · 🔴 **Open**

**Evidence**

> `_add_scope(sub.add_parser("status", help="what is currently halted"))` (halt.py:78) attaches `--scope` (choices: every `HaltScope`) and `--target` to the `status` subcommand, with help text "global stops everything; strategy and symbol need --target" (halt.py:88). `main` then enforces them — `if scope is not HaltScope.GLOBAL and not args.target: raise SystemExit(...)` (halt.py:97-98) — but the status branch is `halts = kill_switch.active_halts()` (halt.py:141), which takes no arguments (`def active_halts(self) -> list[HaltRecord]`, libs/core/src/atp_core/risk/killswitch.py:421) and returns every halt at every scope. Neither `args.scope` nor `args.target` is read after line 95. A scoped query does exist and is unused here: `is_engaged(self, strategy_id: str | None = None, symbol: str | None = None)` (killswitch.py:169).

**Why it matters**

`halt.py status --scope symbol --target SPY` prints the full global halt list and exits `EXIT_HALTED` (2) even when SPY itself is not halted, and conversely reports "HALTED" for a global halt when the operator asked about one symbol. This is the command docs/FIRST_PAPER_RUN.md tells an operator to have ready during an incident, and the exit code is documented as composable with a shell `if` (halt.py:51-54) — so a script gated on a per-symbol check gets the wrong answer.

#### 53. `scripts/halt.py clear` discards the record `KillSwitch.clear` returns and reports success when nothing was halted

`scripts/halt.py:132` · Broken · 🟠 Medium · ⚠️ Reported · 🔴 **Open**

**Evidence**

>     if args.command == "clear":
>         kill_switch.clear(scope, cleared_by=args.by, target=args.target)
>         print(f"Cleared {_describe(scope, args.target)}. Trading may resume.")
>
> The return value is dropped. `KillSwitch.clear` (killswitch.py:79-93) exists to make exactly this distinction: "Returns the halt this call removed, or `None` if nothing was engaged for this scope and target… there is no other race-free way for a caller to tell 'I resumed trading' from 'there was nothing to resume'… which is the difference between an operator being told they restarted the platform and being told they did not." `RedisKillSwitch.clear` (killswitch.py:265-290) is careful to use the DELETE result rather than the GET as the authority for precisely this. The API's `ResumedView` surfaces it as `was_halted`, whose docstring calls it "the field to read first" (risk.py:150-153). The CLI is the only caller that throws it away — while its own header claims "Deliberately thin. Every decision lives in `RedisKillSwitch`" (halt.py:26).

**Why it matters**

An operator running `halt.py clear --by jo` against the wrong scope or target — `--scope symbol --target AAPL` when the halt is global, or a `--target` typo — is told "Cleared symbol AAPL. Trading may resume." and nothing was cleared. The `active_halts()` re-read two lines later mitigates only if that read succeeds and the operator reads past the success line. This is the one command that is documented as the fallback for when the API is down, so it is the path taken when nothing else can confirm the state.

### tests

#### 54. `test_reconstructed_pnl_equals_the_pnl_of_the_fills` checks equality in ~1% of generated examples; the other 99% hit a bound so loose it cannot fail

`tests/unit/test_analytics_performance.py:874` · Broken · 🟠 Medium · ⚠️ Reported · 🔴 **Open**

**Evidence**

> ```python
> 866        net_qty = sum(q * s.sign for s, q, _ in script)
> 867        if net_qty == 0:
> 868            assert closed == self._pnl_from_fills(orders)
> 869        else:
> ...
> 874            assert abs(closed) <= abs(self._pnl_from_fills(orders)) + _open_cost(orders)
> ```
>
> `_open_cost` (line 911-913) is documented as "An upper bound on the cash tied up in whatever is still open" but its body sums the gross notional of **every fill in the script**, closed round trips included:
>
> ```python
> def _open_cost(orders: list[Order]) -> Decimal:
>     return sum((fill.qty * fill.price for placed in orders for fill in placed.fills), Decimal(0))
> ```
>
> With the generator's ranges (qty 1-300, price 50-150, 2-12 orders) that slack is a median of ~2.9x the entire net cash flow, while realised P&L is a fraction of it — the analyzer would have to over-report by roughly 4x before the assertion trips.
>
> How often the exact branch runs, measured by replaying the same hypothesis strategy under the same `@settings(max_examples=250)`:
> ```
> {'zero': 2, 'nonzero': 248}
> ```
> So 248 of 250 examples take the vacuous branch.

**Why it matters**

This is one of the P&L invariants docs/TESTING.md asks for by name ("Any fill sequence: realized + unrealized equals total P&L computed directly") and the file's docstring argues its value comes from `_pnl_from_fills` being a second, independent implementation. In practice the independent check runs on ~2 of 250 examples, and the flip-through-zero and overshoot combinations the docstring says are the reason to generate rather than enumerate almost all land in the non-flat branch — where a `build_trades` bug that mis-attributes 30% of realised P&L on a partially-closed history passes silently. Comparing `closed` against the realised half of `_pnl_from_fills` (or marking the open remainder at its own entry, which the comment at line 869-871 says is the intent) would make the else branch real.

---

## 6. Low severity

Dead code, stale prose, and small inaccuracies. Individually cheap; collectively they
are how a codebase stops being trustworthy to read.

### apps/api

#### 55. The middleware-ordering comment sits on the `add_middleware` call it does not describe

`apps/api/src/atp_api/main.py:216` · Inconsistency · 🟡 Low · ⚠️ Reported · 🔴 **Open**

*Record note (§10, 2026-09-02): Cited `:208` on 2026-08-27; the code is at `:216` today.*

**Evidence**

> ```python
> # Added last, so it runs first. Starlette applies middleware in reverse
> # order of registration, and this one has to wrap CORS rather than sit
> # inside it: ...
> app.add_middleware(
>     CORSMiddleware, ...
> )
> app.add_middleware(ObservabilityMiddleware)
> ```
> The comment describes `ObservabilityMiddleware` — it is the one added last, the one that ends up outermost, and the one that "has to wrap CORS". It is attached to the `CORSMiddleware` call, which is added *first*. The runtime order is correct (`user_middleware` is `[Observability, CORS]`, built in reverse, so Observability is outermost, matching middleware.py:274-278).

**Why it matters**

middleware.py's docstring says the ordering "is set in `main.create_app`" and that the observability layer must stay outermost, and this comment is the only thing recording it there. As written it says CORS is the last-registered/outermost one, so someone reordering these two calls to preserve what the comment claims would move the correlation id and the request metrics inside CORS — which silently stops counting and tracing every request CORS refuses, the exact case the comment says is worth tracing.

#### 56. The API version is a literal in two places; `/` can report a version the OpenAPI doc and `atp_build_info` do not

`apps/api/src/atp_api/main.py:295` · Redundancy · 🟡 Low · ⚠️ Reported · 🔴 **Open**

*Record note (§10, 2026-09-02): Cited `:286` on 2026-08-27; the code is at `:295` today.*

**Evidence**

> `create_app` sets `version="0.1.0"` (main.py:203), which feeds both the OpenAPI document and `build_info(app.version, settings.run_mode.value)` (main.py:79). The root handler restates the same literal instead of reading it back:
> ```python
> return {
>     "name": "ATP",
>     "version": "0.1.0",
> ```
> `app.version` is in scope at that point (the handler is decorated on the module-level `app`).

**Why it matters**

A version bump that touches one literal and not the other leaves `GET /` disagreeing with `/openapi.json` and with the `atp_build_info` label that docs/OBSERVABILITY.md has graphs split by — so a deploy check that reads the root endpoint would confirm a build that is not the one running.

#### 57. `MAX_COMPARE`'s comment names `POST /compare`, a method the endpoint deliberately does not use

`apps/api/src/atp_api/routers/backtests.py:101` · Inconsistency · 🟡 Low · ⚠️ Reported · 🔴 **Open**

**Evidence**

> `#: How many runs `POST /compare` will put side by side.` (backtests.py:101, above `MAX_COMPARE = 8`). The endpoint is `@router.get("/compare", ...)` at backtests.py:834, and its docstring spends a paragraph on the choice: "**A GET, where the skeleton specified `POST /compare`**, and the reason is ADR 0009 rather than taste. `require_write_scope` decides from the method, so as a POST this would be refused with 403 to exactly the reader it is for".

**Why it matters**

The constant's comment restates the rejected design as if it were current. A reader searching for the compare route by the method the constant names finds nothing, and anyone acting on it would reintroduce the POST that ADR 0009 makes unreachable for the read-only session the endpoint exists to serve.

### apps/web (dashboard)

#### 58. lightweight-charts is a declared runtime dependency that nothing in the repo imports

`apps/web/package.json:19` · Redundancy · 🟡 Low · ⚠️ Reported · 🔴 **Open**

**Evidence**

> apps/web/package.json:19 (in "dependencies", not devDependencies):
>     "lightweight-charts": "^4.1.3",
>
> But `grep -rn "lightweight-charts" apps/web --exclude-dir=node_modules` matches only package.json:19 and package-lock.json — zero occurrences in apps/web/src. The two charting components import recharts instead:
>   apps/web/src/components/BacktestDetail.tsx:38  } from 'recharts'
>   apps/web/src/components/EquityChart.tsx:24     } from 'recharts'
> No doc references it either: `grep -rn "lightweight-charts" docs/ README.md` returns nothing.

**Why it matters**

It is installed by `npm ci` in every image stage of infra/docker/web.Dockerfile (dev at line 16, build at line 25) and by `make install`, adding a package nothing uses to the dev container and the build container. It also reads as a second sanctioned charting library alongside recharts, so the next person adding a chart has two options and no stated basis for choosing.

#### 59. apiPatch and apiDelete are exported and called nowhere in the app

`apps/web/src/api/client.ts:83` · Redundancy · 🟡 Low · ⚠️ Reported · 🔴 **Open**

**Evidence**

> client.ts:83-85:
>     export const apiPatch = <T>(path: string, body: unknown) =>
>       request<T>(path, { method: 'PATCH', body: JSON.stringify(body) })
>     export const apiDelete = <T>(path: string) => request<T>(path, { method: 'DELETE' })
>
> `rg 'apiPatch|apiDelete' --glob '*.ts' --glob '*.tsx' src/` matches only these two definitions — no import site, no call, not even in the tests (api/client.test.ts:2 imports only `ApiError, apiGet, apiPost`).
>
> The endpoints they would serve exist server-side but are stubs the UI deliberately does not call: `PATCH /positions/{symbol}/stop` (positions.py:262 → `raise NotImplementedError` at 273) and `DELETE /orders/{order_id}` (orders.py:246, implemented but with no caller — pages/Positions.tsx:128-131 states "No actions ... The endpoints for both are still stubs").

**Why it matters**

Two unexercised verbs on the app's single fetch choke point. They are not covered by any test, so if `request`'s body/credentials handling changed under them the first caller to reach for one would find them broken; and their presence implies write paths exist on screens whose own comments say those actions are deliberately not offered.

#### 60. api/types.ts exports a RunMode union documented as the single restatement of the enum, and nothing imports it

`apps/web/src/api/types.ts:47` · Redundancy · 🟡 Low · ⚠️ Reported · 🔴 **Open**

**Evidence**

> types.ts:39-47:
>     /**
>      * The run modes the UI branches on.
>      * The generated type is a bare `string` ... so this is the one place the
>      * union is restated. ... an unrecognised mode falls through to the loudest
>      * branch rather than to none.
>      */
>     export type RunMode = 'backtest' | 'paper' | 'live'
>
> `rg '\bRunMode\b' --glob '*.ts' --glob '*.tsx' src/` returns three hits: this declaration, and `function RunMode()` / `<RunMode />` in pages/Login.tsx:19,83 — an unrelated local component. No file imports the type.
>
> The two components that branch on the mode compare string literals inline instead: RunModeBanner.tsx:112,119 (`data.run_mode === 'backtest'` / `=== 'paper'`) and Login.tsx:107,114.

**Why it matters**

The union is dead weight that reads as a live invariant. The two banners each hold their own copy of the mode strings, so a fourth mode added server-side changes neither of them and neither is a compile error — which is the exact drift the comment claims this type prevents.

#### 61. The risk panel's client-side ceiling fallback re-implements the server's row set and disagrees with it on rate_limit

`apps/web/src/components/RiskLimitsPanel.tsx:126` · Inconsistency · 🟡 Low · ⚠️ Reported · 🔴 **Open**

**Evidence**

> RiskLimitsPanel.tsx:118-137 `ceilingRows` builds all six LimitUsageView rows with `observable: true` (line 126) and `note: null` (line 127), including `of('rate_limit', 'orders_per_minute', ...)`.
>
> The server builds the same six rows for the same "no readings" case — apps/api/src/atp_api/routers/risk.py:442-466 `_unreadable_rows` — with `observable=rule != "rate_limit"` and, for that rule, `note=_RATE_LIMIT_NOTE`. Its docstring: "`rate_limit` keeps `observable=False` here rather than joining the others at null, because the two mean different things and the distinction survives the book coming back: the rest are unknown *right now*, that one is unknown always."
>
> The panel's own `Verdict` (line 88-91) branches on exactly that flag: `if (!row.observable) return 'not observable'` before `if (row.at_limit === null) return 'no reading'`.

**Why it matters**

When /risk/status returns 503 the panel falls back to /risk/limits and renders the order-rate row as "no reading" — implying a reading exists and is momentarily missing — instead of "not observable" with the server's explanation that the API can never report it (the rule's window lives in the worker and counts refused attempts). The panel's own header comment lists "not observable" as one of the four words a row must be able to say; in the degraded path it can never say it.

#### 62. Analytics justifies its typed strategy field with "/strategies is still a stub", which is false

`apps/web/src/pages/Analytics.tsx:192` · Inconsistency · 🟡 Low · ⚠️ Reported · 🔴 **Open**

**Evidence**

> Analytics.tsx:191-195:
>     {/* Typed rather than picked from a list: `/strategies` is still a
>         stub, so there is no directory of them to read. ... */}
>
> `GET /api/v1/strategies` is implemented — apps/api/src/atp_api/routers/strategies.py:291 `@router.get("", response_model=StrategiesResponse)` with a real body (only `/available`, `/{id}`, PATCH, promote and pause are `raise NotImplementedError`). The front end already consumes it: hooks/useStrategies.ts:126-135 `useStrategies`, used by pages/Strategies.tsx:220 and by pages/Backtests.tsx:54, which builds a working strategy picker from it via `strategyChoices`.

**Why it matters**

The premise for making this the only free-text strategy field in the app is gone. A reader maintaining the screen is told a directory of strategies cannot be fetched when the identical picker is two tabs away, so the free-text box — where a typo silently returns an empty report rather than a 422 — stays for a reason that no longer exists.

#### 63. Strategies page tells operators the promotion preconditions cannot be checked, naming two that now can

`apps/web/src/pages/Strategies.tsx:361` · Inconsistency · 🟡 Low · ⚠️ Reported · 🔴 **Open**

**Evidence**

> Strategies.tsx:358-362 renders to the screen: "Read-only. Creating, editing, promoting and pausing a strategy are the promotion ratchet — draft → backtest → paper → live — and half its preconditions cannot be checked yet: there is no stored backtest to require, and the audit trail cannot yet record who promoted what."
>
> Both halves are out of date. Backtests are stored and readable — this same app has a Backtests tab reading `GET /api/v1/backtests` (hooks/useBacktests.ts:116). The server's own promote handler says so: apps/api/src/atp_api/routers/strategies.py — "'A completed backtest on record' became checkable when `backtest_runs` got a reader (ADR 0016). The audit trail is no longer the blocker either: `create_strategy` above writes a lifecycle verb naming the session's user, so the mechanism this needed — a verb, an actor from the cookie rather than from the body, a sink that never fails the action — is wired and demonstrated."
>
> The same claim is repeated in the module docstring, Strategies.tsx:36-38.

**Why it matters**

This is user-facing copy, not a code comment. It tells an operator two capabilities are missing that the platform has, which misdirects anyone asking why promotion is unavailable — the real remaining blocker, per the server, is the per-transition verbs and the unrecorded paper-trading start date, not the backtest record or the audit trail.

### apps/worker

#### 64. apscheduler is declared as a worker dependency but is never imported — the scheduler is hand-rolled

`apps/worker/pyproject.toml:9` · Redundancy · 🟡 Low · ⚠️ Reported · 🔴 **Open**

**Evidence**

> apps/worker/pyproject.toml:6-10
>   dependencies = [
>     "atp-core",
>     "arq>=0.26",
>     "apscheduler>=3.10",
>   ]
>
> apps/worker/src/atp_worker/scheduler.py:271-277 says outright that it is not used:
>   # Hand-rolled rather than apscheduler, which this package already depends on.
>   # Two of the four trigger types here are relative to a *session* — five minutes
>   # before the open, thirty after the close — and neither a cron nor an interval
>   # trigger can express either ...
>
> `grep -rn "apscheduler" --include="*.py"` over apps/, libs/, tests/ and scripts/ returns only those two comment lines in scheduler.py — no import statement anywhere in the repository.

**Why it matters**

It is installed into both the worker and queue containers (both built from infra/docker/worker.Dockerfile, which runs `uv sync --package atp-worker`) and pinned in uv.lock, so it carries CVE and resolution surface for code that does not exist. The comment's phrasing "which this package already depends on" also invites the next contributor to reach for it, re-opening a decision the same comment explains was already made against.

### docs

#### 65. README.md's documentation index links to docs/API.md, which does not exist

`README.md:89` · Broken · 🟡 Low · ✅ Verified · 🔴 **Open**

**Evidence**

> README.md:89: "| [API.md](docs/API.md) | REST/WS surface and conventions |". `ls docs/API.md` → No such file or directory; it is the only unresolved markdown link in the entire repository (checked every relative link in all 41 markdown files) and the only unresolved `docs/*.md` reference from source, Makefile or compose. No other file references API.md, so nothing was renamed — the page was never written.

**Why it matters**

The README's documentation table is the routing layer for every other page, and this is the row a reader follows to learn the REST/WS conventions before writing a client or an endpoint. The link 404s, and there is no substitute page named — the API surface is documented only in scattered sections of DASHBOARD.md, ANALYTICS.md and BACKTESTING.md.

#### 66. DASHBOARD_STATUS.md's provenance paragraph cites a `StrategyKind` enum that does not exist and undercounts the audit verbs by six

`docs/DASHBOARD_STATUS.md:17` · Inconsistency · 🟡 Low · ⚠️ Reported · 🔴 **Open**

*Record note (§10, 2026-09-02): Its own count has drifted: the file names five verbs against **12**, so it undercounts by seven, not six.*

**Evidence**

> docs/DASHBOARD_STATUS.md:14-18: "every enum value checked against the domain rather than guessed (`OrderStatus`, `SignalAction`, `StrategyKind`, the four backtest statuses in `atp_core.backtest.ports`, and the five audit verbs in `atp_core.audit.ports`)."
>
> `rg StrategyKind` matches only this line — libs/core/src/atp_core/domain/enums.py declares RunMode, Side, OrderType, TimeInForce, OrderStatus, SignalAction, StopType, Timeframe and `StrategyState` (:135), which is the enum the strategies screen actually renders (see this same doc at :131-133, which names `StrategyState` correctly). libs/core/src/atp_core/audit/ports.py declares eleven verbs, not five: LOGIN, LOGIN_FAILED, LOGOUT, RATE_LIMITED, FORBIDDEN, HALT_ENGAGED, HALT_CLEARED, STRATEGY_CREATED, ORDER_CANCELLED, POSITION_CLOSED, FLATTEN_ALL. (The four backtest statuses at ports.py:46-49 are correct.)

**Why it matters**

This paragraph is the doc's warrant — it tells the reader how much to trust everything below by naming what was checked against the domain. Two of the three enumerations it cites are wrong, one of them by name, which means the audit page's fixture coverage was reasoned about from a five-verb vocabulary while six more verbs (including both halt verbs and the flatten-all verb) can appear on that screen.

#### 67. RUNBOOK.md tells the operator `make status` works; there is no such Makefile target

`docs/RUNBOOK.md:349` · Broken · 🟡 Low · ⚠️ Reported · 🔴 **Open**

*Record note (§10, 2026-09-02): Cited `:325` on 2026-08-27; the code is at `:349` today.*

**Evidence**

> docs/RUNBOOK.md:325-327: "`make preflight` and `make status` no longer die on this. They used to call `get_settings()` and exit with the same traceback they were being run to explain; they now say which variable will not load and point here."
>
> Makefile has `preflight:` (line 162) but no `status:` target — the full target list is help, install, .env, up, up-prod, deploy, secrets-check, secrets-install, backup, backup-verify, backup-list, backup-restore, down, logs, migrate, revision, seed, backfill, check-env, preflight, paper-report, test, test-unit, test-integration, lint, typecheck, fmt, check-tracked, check-bindings, check, gen-types, dev-api, dev-worker, dev-web, build-web, clean. Every other RUNBOOK reference to this tool uses the script path correctly (`scripts/status.py` at :45, :57, :202).

**Why it matters**

This is the incident runbook, and `scripts/status.py` is the tool it recommends as "the read-only companion… safe to run during an incident". An operator who types the form the runbook prints gets `make: *** No rule to make target 'status'` at the moment they are trying to find out what the platform believes is true.

#### 68. TESTING.md documents a `sample_bars` fixture that does not exist

`docs/TESTING.md:69` · Broken · 🟡 Low · ✅ Verified · 🔴 **Open**

*Record note (§10, 2026-09-02): Cited `:58` on 2026-08-27; the code is at `:69` today.*

**Evidence**

> docs/TESTING.md:56-60 ("## Fixtures") lists "`fake_broker`", "`sample_bars` — deterministic OHLCV, including a gap and a split", "`frozen_clock`". tests/conftest.py defines `utc_now` (:77), `frozen_clock` (:82), `fake_broker` (:90) and `empty_portfolio` (:104) — there is no `sample_bars`, and `rg sample_bars` across the repo matches only this doc line. `empty_portfolio`, which does exist, is not listed.

**Why it matters**

A contributor writing a bar-driven test — the most common kind in this repo — requests `sample_bars` on the strength of this list and gets a pytest fixture-not-found error, then hand-rolls bar data. The gap-and-split series the doc promises is exactly the fixture that would make corporate-action and gap-detection tests consistent, and the doc asserts it is already there.

### infra / config / CI

#### 69. The .gitignore rule apps/web/dist/ is dead, already covered by the unanchored dist/ five lines above it

`.gitignore:46` · Redundancy · 🟡 Low · ⚠️ Reported · 🔴 **Open**

**Evidence**

> .gitignore:37-38 (python section), both unanchored so they match at every depth:
>   37: dist/
>   38: build/
>
> .gitignore:46 (node section) then repeats a subset of line 37:
>   46: apps/web/dist/
>
> The file's own stated policy is that depth-matching rules are the hazard, and it is stated twice — at lines 15-16 ("Anchored with a leading slash, per the lesson spelled out in the data/ block below") and at 52-58 ("ANCHOR THESE WITH A LEADING SLASH. An unanchored `data/` matches at every depth, and this repo has a source package at libs/core/src/atp_core/data/ — the entire market-data layer was silently excluded ..."). Lines 37-38 are the two rules that do not follow it.

**Why it matters**

Line 46 has no effect — line 37 already excludes apps/web/dist at any depth — so a reader editing the node section believes they control whether the web bundle is ignored when they do not, and removing line 46 to un-ignore a dist path would change nothing. The unanchored `build/` at line 38 is the live version of the exact hazard the data/ block documents: any future source directory named build/ anywhere under libs/ or apps/ disappears from the repository the same way libs/core/src/atp_core/data/ did.

#### 70. .PHONY omits check-tracked, preflight and paper-report

`Makefile:2` · Inconsistency · 🟡 Low · ✅ Verified · 🔴 **Open**

**Evidence**

> Makefile:2-6
>   .PHONY: help install up up-prod deploy down logs migrate revision seed backfill \
>           secrets-check secrets-install backup backup-verify backup-list backup-restore \
>           test test-unit \
>           test-integration lint typecheck fmt check check-bindings check-env gen-types build-web \
>           dev-api dev-worker dev-web clean
>
> Three non-file targets are defined but absent from that list:
>   Makefile:162  preflight:  ## Is this configuration ready for a paper week?
>   Makefile:165  paper-report:  ## What the paper run demonstrated: ...
>   Makefile:203  check-tracked:  ## Fail if any source file is excluded by .gitignore
>
> check-tracked is a prerequisite of the repo's primary gate:
>   Makefile:230  check: check-tracked lint typecheck test  ## Everything CI runs — green before you push

**Why it matters**

Make treats an undeclared target as a file rule. A file or directory named `check-tracked`, `preflight` or `paper-report` appearing in the repo root — a script someone drops there, a stray output file — makes Make consider the target up to date and skip the recipe. For check-tracked that silently removes the gitignore-source gate from `make check` while `make check` still reports success, and since that gate runs nowhere in CI either (see the check-tracked/CI finding) nothing else would catch it.

#### 71. Nothing verifies the generated TS types are current, though the contract depends on it

`Makefile:265` · Inconsistency · 🟡 Low · ✅ Verified · 🔴 **Open**

*Record note (§10, 2026-09-02): Cited `:246` on 2026-08-27; the code is at `:265` today.*

**Evidence**

> `make gen-types` dumps the OpenAPI schema and regenerates `apps/web/src/api/schema.d.ts`. `apps/web/src/api/types.ts:13-18` explains the whole point: 'a hand-maintained duplicate of a server contract drifts, and the drift shows up as a runtime `undefined` inside a P&L figure rather than as a compile error (CLAUDE.md §4)'. No CI job runs `gen-types` and diffs the result: the `web` job runs lint, format:check, typecheck, test and build; the `python` job runs ruff, mypy and pytest. I regenerated both artefacts in this session and they came back byte-identical, so there is no drift today — but only discipline is keeping it that way.

**Why it matters**

A contributor who changes a response model and forgets `make gen-types` gets a green CI. The stale type then describes a payload the server no longer sends, and because the generated types are the only definition the dashboard has, `tsc` cannot see the disagreement either. The failure surfaces at runtime as an undefined field — which is exactly the outcome `types.ts` says the generation step exists to prevent.

### libs/core — backtest

#### 72. ADR 0019 enumerates nine fields for `backtest_runs.totals`; the writer and the API model both have ten — `starting_equity` is missing from the ADR

`libs/core/src/atp_core/backtest/engine.py:264` · Inconsistency · 🟡 Low · ✅ Verified · 🔴 **Open**

**Evidence**

> libs/core/src/atp_core/backtest/engine.py:263-274 — `BacktestResult.totals()` returns ten keys, six of them decimal strings:
>
>         return {
>             "starting_equity": str(self.portfolio.starting_equity),
>             "ending_equity": str(self.portfolio.equity),
>             "total_return": str(self.total_return),
>             "realized_pnl": str(self.realized_pnl),
>             "unrealized_pnl": str(self.unrealized_pnl),
>             "fees": str(sum((o.total_fees for o in self.orders), Decimal(0))),
>             "open_positions": len(self.portfolio.open_positions),
>             "orders": len(self.orders),
>             "filled_orders": len(filled),
>             "signals": len(self.signals),
>         }
>
> apps/api/src/atp_api/routers/backtests.py:224-235 declares all ten, `starting_equity: str` first and non-optional.
>
> docs/adr/0019-a-run-s-money-is-not-a-metric.md:7-10 lists nine and omits `starting_equity`: "`ending_equity`, `total_return` as a decimal string, `realized_pnl`, `unrealized_pnl`, `open_positions`, `orders`, `filled_orders`, `signals`, `fees`"; line 49: "one additive nullable JSON column holding all nine"; line 55: "Five of the nine are money and four are counts". The same miscount is copied into libs/core/src/atp_core/backtest/runner.py:366-367 ("`metrics` is float by contract and five of these are money") and libs/core/src/atp_core/persistence/models.py:334.

**Why it matters**

The ADR is the specification a second writer would be built against, and the ADR's own Consequences section says "The API validates `totals` against a fixed model rather than passing the bag through... a row that does not fit the model is a disagreement between the writer and the schema and should be loud." A producer written to the ADR's nine-field list omits `starting_equity`, and because `BacktestTotalsView.starting_equity` is a required non-optional field, every read of that run then 500s at `BacktestTotalsView.model_validate(run.totals)` (apps/api/src/atp_api/routers/backtests.py:379) — the loud failure the ADR predicts, caused by the ADR itself. The "five of the nine are money" arithmetic is also wrong as stated: `totals()` emits six decimal strings and four integers.

**Verification note**

Confirmed by execution: `BacktestTotalsView` has 10 required fields, and validating a payload built from ADR 0019's nine-field list raises `1 validation error ... starting_equity Field required`. The ADR's arithmetic is also off — `totals()` emits six decimal strings and four integers, not ‘five money and four counts’.

#### 73. `BacktestEngine._fill_pending` is unreachable — the loop calls `_fill_pending_for` directly

`libs/core/src/atp_core/backtest/engine.py:951` · Redundancy · 🟡 Low · ⚠️ Reported · 🔴 **Open**

**Evidence**

>     def _fill_pending(self, bar: Bar) -> list[Order]:      # engine.py:951
>         """Fill resting orders against this bar. ..."""
>         result = self._result
>         if result is None:  # pragma: no cover - run() always sets it
>             raise BacktestError("_fill_pending called outside run()")
>         return self._fill_pending_for(bar, result)
>
> `rg -n "_fill_pending\b"` across libs/, apps/, scripts/ and tests/ returns only this definition (engine.py:951) and its own error string (engine.py:961). The run loop calls `self._fill_pending_for(bar, result)` at engine.py:639, and nothing else references the wrapper — including the `self._result` field it exists to read.

**Why it matters**

It is a wrapper that adds nothing, and it carries the documented statement of the fill rule ('Market → next open plus slippage. Limit → only if the bar's range actually reached the price...') on a method no execution path reaches, so the rule's description sits on dead code while the live path is `_fill_pending_for`/`_intended_price`. Anyone editing the fill semantics here would change nothing.

#### 74. `NO_RISK_RULES_WARNING` is dead, and `build_engine`'s docstring promises it is attached when it never is

`libs/core/src/atp_core/backtest/runner.py:99` · Redundancy · 🟡 Low · ⚠️ Reported · 🟢 **Closed** — @claude (#111)

*Record note (§10, 2026-09-02): Cited `:93` on 2026-08-27; the code is at `:99` today.*

**Evidence**

> Defined at runner.py:93-96:
>
>     NO_RISK_RULES_WARNING = (
>         "no pre-trade risk rules were active — orders were routed through "
>         "RiskEngine, but nothing refused them"
>     )
>
> `rg -n "NO_RISK_RULES_WARNING"` across the repo returns the definition and nothing else — `run_spec` (runner.py:320-350) attaches `ZERO_COST_WARNING`, `FIXED_QTY_WARNING` and `refusal_summary` only. Yet `build_engine`'s docstring at runner.py:275-277 states:
>
>     `rules=[]` is still reachable, and still deliberate: `with_rules=False` is
>     how a caller asks for an engine that refuses nothing, and the warning goes
>     back on the result when they do.
>
> `with_rules` also has no caller anywhere: `rg -n "with_rules"` returns only the parameter (runner.py:259) and its use at runner.py:309.

**Why it matters**

The docstring is a promise the code does not keep: a caller that takes `build_engine` at its word and passes `with_rules=False` gets a run whose orders nothing refuses and whose result says nothing about it — the exact silent-flattery failure the surrounding paragraph argues against. Meanwhile the constant reads as live wiring to anyone grepping for what a result can warn about.

### libs/core — errors

#### 75. `StaleDataError` and `KillSwitchEngagedError` are never raised or caught anywhere

`libs/core/src/atp_core/errors.py:74` · Redundancy · 🟡 Low · ✅ Verified · 🔴 **Open**

*Record note (§10, 2026-09-02): Cited `:41` on 2026-08-27; the code is at `:74` today.*

**Evidence**

> `rg 'StaleDataError'` over `libs`, `apps`, `scripts` and `tests` returns exactly two hits: the class definition at `errors.py:41` and a docstring at `risk/rules.py:354` that claims 'this rule is why `StaleDataError` exists'. `StaleDataRule.check` (rules.py:362-379) returns `RiskDecision.deny(...)` on both its refusal paths and raises nothing. `KillSwitchEngagedError` (errors.py:84) has zero references outside `errors.py`; `RedisKillSwitch` signals through `is_engaged() -> bool` and `KillSwitchRule` denies by return value. Checked every exception in `errors.py` this way — these two are the only ones with no reference outside the module.

**Why it matters**

Refusing by return value is the deliberate design (`router.py` header: 'A refusal is a return value; an indeterminate submit is an exception'), which makes both exceptions vestigial. The `rules.py:354` comment is the active harm: it tells a reader the rule raises, which would send someone writing a `try/except StaleDataError` around a call that can never raise one.

### libs/core — indicators

#### 76. `ta.crossed_above` / `crossed_below` are dead, and `sma_crossover` reimplements them inline

`libs/core/src/atp_core/indicators/ta.py:285` · Redundancy · 🟡 Low · ✅ Verified · 🔴 **Open**

**Evidence**

> `libs/core/src/atp_core/strategy/examples/sma_crossover.py:67-68` writes the comparison by hand:
>
>     crossed_up = fast_prev <= slow_prev and fast_now > slow_now
>     crossed_down = fast_prev >= slow_prev and fast_now < slow_now
>
> which is character-for-character what `ta.crossed_above` (ta.py:285) and `ta.crossed_below` (ta.py:295) already do. `crossed_above`'s own docstring says 'A crossover is an edge, not a level — see the note in the SMA example', so the pair was written together. Neither is reachable from the declarative rule spec either: `indicators/dispatch.py:29` limits `KNOWN_INDICATORS` to `{sma, ema, rsi, stddev, atr}`. `rg` finds no caller for `crossed_below` anywhere — not in `libs`, `apps`, `scripts` or `tests` — and `crossed_above` only in `test_indicators.py`.

**Why it matters**

Two implementations of one predicate, and the one with no test (`crossed_below`, which also lacks the docstring its twin has) is the one a strategy author would find first via autocomplete. `macd`, `macd_series`, `bollinger` and `bollinger_series` are in the same position: implemented in `ta.py`, absent from `KNOWN_INDICATORS`, and referenced only by tests.

### libs/core — persistence

#### 77. PostgresBacktestRunRepository.create() writes every backtest_runs column except `totals`

`libs/core/src/atp_core/persistence/backtests.py:76` · Inconsistency · 🟡 Low · ⚠️ Reported · 🔴 **Open**

**Evidence**

> create() enumerates the row explicitly:
>
>     BacktestRunRow(
>         id=run.id, strategy_id=run.spec.strategy_id, config=spec_to_json(run.spec),
>         status=run.status, metrics=run.metrics, equity_curve=run.equity_curve,
>         trades=run.trades, warnings=run.warnings, error=run.error,
>         queued_at=run.queued_at, started_at=run.started_at, finished_at=run.finished_at,
>     )
>
> `BacktestRunRow` has 13 columns; this names 12. `totals` (models.py:338, added by migration f1b7c0d4e295) is the one omitted. Every other result column on `StoredBacktestRun` is passed through.
>
> It is the only incomplete insert in the persistence layer — I checked the others mechanically: orders.py `_order_values` writes all 23 OrderRow columns, positions.py `snapshot()` writes all 13 non-id PositionSnapshotRow columns.
>
> The rest of this same file handles `totals`: `finish()` writes it (backtests.py:150), `fail()` clears it (backtests.py:172), `_to_stored()` reads it back (backtests.py:283). And the in-memory double does not drop it — tests/fakes.py:776 `self.runs[run.id] = run` stores the whole StoredBacktestRun, `totals` included.

**Why it matters**

Latent today only because `new_run()` (backtests.py:292) is the sole producer fed to `create()` and it leaves `totals` at its None default. The moment anything constructs a StoredBacktestRun with totals already populated and calls `create` — a re-import of an exported run, a seed script, a replay — the money figures are silently dropped on insert, and no test catches it because FakeBacktestRunRepository preserves the field the Postgres adapter discards. This is the exact failure mode `spec_to_json`'s docstring warns about ('a field on the spec is a field here') applied to the row instead of the spec.

#### 78. _reject_floats recurses into nested dicts but not into lists, so a float inside any list is published unchecked

`libs/core/src/atp_core/persistence/events.py:79` · Broken · 🟡 Low · ✅ Verified · 🔴 **Open**

**Evidence**

> The guard body (events.py:70-80):
>
>     for key, value in message.items():
>         where = f"{path}{key}"
>         if isinstance(value, bool):
>             continue
>         if isinstance(value, float):
>             raise ValueError(...)
>         if isinstance(value, dict):
>             _reject_floats(channel, value, f"{where}.")
>
> There is no `isinstance(value, list)` branch. `publish({"prices": [1.5]})` or `publish({"points": [{"price": 1.5}]})` passes the guard and reaches `json.dumps` at events.py:54.
>
> This contradicts the function's own docstring two lines above (events.py:60-61): "The guard exists because this is the last place a price can be checked before it leaves the process."
>
> The hole is untested: tests/unit/test_redis_adapters.py has `test_refuses_to_publish_a_float` (top-level, line 211) and `test_refuses_a_float_nested_in_a_sub_document` (dict, line 223) — no list case.

**Why it matters**

Not reachable from today's producers (runner._signal_message, runner._fill_message and stream._quote_message are all flat string dicts), so this is a guard with a hole rather than a live corruption. But the first list-shaped payload — a mini equity series, a list of position documents, a batch of bars — bypasses rule §1.1 entirely, and by the docstring's own account the corruption surfaces on a dashboard as a price ending in a run of 9s long after the code shipped. The dict branch shows the recursive intent; the list branch is simply missing.

**Verification note**

Proven by execution. `_reject_floats('ch', {'prices': [101.25]})` and `_reject_floats('ch', {'positions': [{'qty': 100.0}]})` both return without raising, and `json.dumps` then encodes them as JSON numbers. Top-level and dict-nested floats are caught correctly.

### scripts

#### 79. `--adjusted` in the backfill script is a dead flag: never read, and it cannot be false

`scripts/backfill_bars.py:52` · Redundancy · 🟡 Low · ⚠️ Reported · 🔴 **Open**

**Evidence**

> scripts/backfill_bars.py:52:
>
>     p.add_argument("--adjusted", action="store_true", default=True)
>
> `args.adjusted` is never read — `rg 'args\.adjusted' scripts/backfill_bars.py` returns nothing. The adjusted pass is driven entirely by the other flag:
>
>     backfill_bars.py:159   adjusted=not args.raw_only,
>     backfill_bars.py:177   adjusted=not args.raw_only,
>
> And because `store_true` is paired with `default=True`, the attribute is `True` on every invocation regardless — there is no `--no-adjusted`, so passing or omitting it is indistinguishable.

**Why it matters**

`--help` advertises a switch over the single most consequential property of what gets written (ADR 0017 makes a missing `adj_close` refuse a backtest outright). An operator who reasons "I want adjusted closes, so I'll pass `--adjusted`" and one who reasons "I'll drop `--adjusted` to save requests" both get exactly the same behaviour, and neither learns that `--raw-only` is the real control.

#### 80. `_parse_day` is duplicated verbatim across two scripts, and seed.py's copy claims a uniqueness that is false

`scripts/run_backtest.py:142` · Redundancy · 🟡 Low · ⚠️ Reported · 🔴 **Open**

*Record note (§10, 2026-09-02): Cited `:135` on 2026-08-27; the code is at `:142` today.*

**Evidence**

> scripts/run_backtest.py:135-141 and scripts/backfill_bars.py:71-77 are byte-identical apart from one word of the docstring: both are `def _parse_day(value: str, field: str) -> datetime:` wrapping `datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=UTC)` and raising `SystemExit(f"--{field} must be YYYY-MM-DD, got {value!r}")`. A third copy is scripts/seed.py:124-131 (`parse_day`, same body, returning `.date()`), whose docstring asserts "A calendar day. Naive input is fine here **and only here**: these are days, not instants" — contradicted by the two copies above doing exactly the same naive-string-to-UTC conversion. A fourth partial copy is scripts/paper_report.py:112-118 (`_since`).

**Why it matters**

Four places parse the same operator-facing date format with the same error message and no shared helper, so a change to the accepted format or the message reaches one script and not the others — and seed.py's "and only here" comment actively misleads the next reader into believing the pattern is confined to that file.

### tests

#### 81. The e2e tier is documented, marker-registered and directory-scaffolded, but holds zero tests and no target or CI job runs one

`tests/e2e/__init__.py:1` · Inconsistency · 🟡 Low · ✅ Verified · 🔴 **Open**

*Record note (§10, 2026-09-02): **Cites an absence.** This citation has never resolved. `tests/e2e/__init__.py` has been zero bytes since it was committed, so there is no line 1 and never was. Read it as "the directory these tests are missing from".*

**Evidence**

> `wc -c tests/e2e/__init__.py` → `0`. The directory contains nothing else.
>
> Against that, docs/TESTING.md:11 presents it as an existing tier — "`e2e/   full stack against the paper account. @pytest.mark.e2e`" — and pyproject.toml:125 registers the marker (`"e2e: full stack"`). `rg "mark.e2e" tests/` returns nothing; `rg "e2e" .github/ Makefile scripts/` returns nothing at all, so no CI job and no make target ever names the tier. `make test` runs bare `uv run pytest` with `testpaths = ["tests"]`, which walks tests/e2e/ and silently collects zero.
>
> (The `slow` marker at pyproject.toml:126 is registered and likewise applied nowhere.)

**Why it matters**

docs/TESTING.md is the page a contributor reads to learn what the suite covers, and its layout block reads as a description of what exists rather than of what is planned. Nothing in the repo would notice if the tier stays empty forever: there is no failing job, no empty-collection guard of the kind CI already added for the integration step ("an empty collection means they stopped being collected, which is a failure"), and no roadmap item that names it. docs/ROADMAP.md shows Phase 4 (the paper week) at 0/10, so an empty tier is consistent with the platform's state — but the docs and the pytest config assert a capability the repository does not have.

#### 82. `CORE_SUBPACKAGES` says it is "every subpackage the platform is built from" but omits four of the fourteen, including two named in CLAUDE.md's package table

`tests/unit/test_repo_integrity.py:21` · Inconsistency · 🟡 Low · ⚠️ Reported · 🔴 **Open**

*Record note (§10, 2026-09-02): Its own count has drifted: `atp_core` now has **15** subpackages, so the list omits five, not four.*

**Evidence**

> ```python
> 19 #: Every subpackage the platform is built from. Adding one here is deliberate —
> 20 #: it is the list that says "this must exist in a fresh checkout".
> 21 CORE_SUBPACKAGES = [
> 22     "analytics", "backtest", "brokers", "data", "domain",
> 27     "execution", "indicators", "persistence", "risk", "strategy",
> 32 ]
> ```
>
> `ls libs/core/src/atp_core/` shows fourteen packages: alerts, analytics, audit, backtest, brokers, dashboard, data, domain, execution, indicators, metrics, persistence, risk, strategy. Missing from the list: **alerts, audit, dashboard, metrics** — and CLAUDE.md §2's core package table names `alerts/` ("the alert port and its transports") and `metrics/` ("every metric name in the platform, declared once") explicitly.
>
> `PORT_MODULES` (line 36-39) is likewise two entries — `atp_core.brokers.ports`, `atp_core.data.ports` — against the several `ports.py` modules the repo actually has (execution, strategy, backtest, audit).

**Why it matters**

The file's docstring says it exists because an unanchored `.gitignore` rule (`data/`) once excluded a whole core package from the repository and only CI noticed. CI runs lint, mypy, unit and integration — it does not run `make check-tracked`, so within CI this list is the check. An unanchored rule matching `alerts/`, `audit/`, `dashboard/` or `metrics/` is not covered by the list that claims to cover everything; the comment tells the next contributor the invariant is already held for their new package when it is not.

---
## 7. Checked and found clean

A negative result is worth as much as a finding, and several of these are places
where a plausible-sounding defect turned out to be a deliberate, well-documented
design. Recording them stops the next audit re-deriving them.

| Check | Method | Result |
|---|---|---|
| ORM ↔ migration drift | Replayed all 10 migrations' `upgrade()` bodies with an AST walker and diffed column sets against `Base.metadata` | **Exact parity** on all 9 tables. Remaining "mismatches" were `VARCHAR(20)` vs `String(20)` — the same type |
| Migration chain integrity | `alembic heads` + `down_revision` graph | Single linear chain, 10 revisions, one head |
| `Settings` ↔ `.env.example` | Enumerated Pydantic fields and aliases, diffed against parsed `.env.example` | All 54 declared keys present. The 4 extras (`VITE_API_BASE_URL`, `VITE_WS_URL`, `ATP_DEV_PROXY_TARGET`, `ATP_WEB_BIND_ADDR`) are each consumed by `vite.config.ts`, `origin.ts`, compose or the Makefile |
| Generated TS types | Ran `dump_openapi.py` + `openapi-typescript`, diffed against the committed file | **Byte-identical** — no drift today (but see finding on enforcement) |
| API auth coverage | Enumerated every route and its dependencies | Sound. Allow-list at `main.py:245`; `test_api_contract.py` holds the same line from outside, so neither can drift alone |
| Hand-written API payload types | Read `apps/web/src/api/types.ts` | §4-compliant — pure aliases over `components['schemas']`, with one documented exception (`RunMode`) |
| Risk rule registration | Cross-referenced rule classes against `default_rules()` | All 9 wired. `anchor_session` is called by both the backtest engine and the worker |
| Vacuous tests | AST-scanned all 2,161 test functions for missing assertions | 22 have no `assert` — all are deliberate "does not raise" tests whose subject raises on failure. No vacuous tests found by this method (but see findings 14, 21 and 54, which are vacuous for a different reason) |
| Dead modules in the dashboard | Checked every module under `apps/web/src` for a non-test importer | None. Every module is imported |
| Scaffolding left in source | `rg` for `TODO`/`FIXME`/`XXX`/`HACK` | 2 hits, both historical prose in a test docstring |
| Secrets in the tree | `.env` gitignored; `manage_secrets.py` fails with a helpful message when no bundle exists | No secrets committed. `gitleaks` runs in CI |

## 8. Limitations of this audit

Stated plainly, because an audit that overstates its coverage is worse than a
shorter one.

1. **The adversarial verification pass did not run.** 57 of the 82 findings are
   marked ⚠️ and have not been independently re-checked. I verified 25 myself,
   including 8 of the 14 high-severity findings. Verify before acting, and
   especially before changing risk or execution code.
2. **One of thirteen reviewers did not finish** — the repository-wide dead-code
   and duplication sweep over `libs/core`. Partial coverage of that dimension came
   from my own AST-based unused-symbol scan, which is how findings 75 and 76 were
   found. Expect more dead code than this report lists.
3. **No Postgres.** No Docker daemon and no TimescaleDB extension in this
   environment, so the integration tier largely skipped and the `stack` CI job
   could not be reproduced. The ORM/migration comparison above is static, not a
   real `alembic upgrade head`.
4. **Nothing was executed against a broker or a live feed.** Consistent with
   CLAUDE.md §1.7, but it means the reconnect, reconciliation and partial-fill
   paths were read rather than exercised.
5. **Severities are mine, and assume the platform reaches production.** Several
   high findings are latent: Phase 4 is entirely unticked on the roadmap and
   nothing has traded a paper account, so the execution defects have not yet had
   the chance to cost anything. That is a statement about timing, not about
   severity. (This read "Phase 4 is 0/10" until §10. It was true on 2026-08-27
   and false six days later, when #127 split an item and made it 0/11 — a count
   copied out of another document drifts the moment that document is edited, so
   the count is gone and the fact is stated instead.)

## 9. Suggested order of work

Nothing here was changed — this is a read-only audit. If it were mine to fix:

1. **Findings 1, 4, 5 and 6 first.** They are one theme — the live trading loop:
   the strategy never sees a bar (6), an entry that did fire could not be sized (4),
   a trailing stop does not trail (5), and the operator's close-out leaves a live
   stop at the venue (1). Together they mean the paper week the roadmap is waiting
   on would fail on its first day, in ways that would be hard to read from the logs.
   All four sit at the runner-to-router seam, which `test_strategy_runner.py` drives
   with a router double — that is the gap that let them through, and it is what
   needs a test.
2. **Finding 12** (reconnect-backfilled bars are permanently raw-only) before any
   long paper run, because it silently poisons the history every later backtest reads.
3. **Findings 14, 21 and 54** — the tests that cannot fail. One is the *only*
   wire-level guard on CLAUDE.md §1.1, and it is vacuous for nullable fields.
4. **The docs block.** Seventeen findings across §4–§6, most of them prose that
   describes a platform that no longer exists — stubs that were implemented, a rate
   limiter said not to exist, a promotion ratchet said to be enforced. This is
   cheap to fix and it is what a new contributor reads first.
5. **The low-severity dead code**, opportunistically.

## 10. The record review — 2026-09-02

This section is the result of auditing the 82 findings above against the record
conventions `docs/ROADMAP.md` sets out under *How this file is maintained*, and
`CLAUDE.md` §6 requires. Reviewed at `4f68cf4`, six days and nineteen merged
pull requests (#109–#127) after the audit commit.

The premise is the roadmap's own: **a status document is worthless the moment it
lags the code.** That sentence was written about `docs/ROADMAP.md`, but nothing
in it is specific to a roadmap. This file is a status document too — 82 claims
about what is wrong with a tree — and it had none of the machinery the roadmap
has grown to keep such claims honest.

### 10.1 The conventions, and how this file scored

| | Convention (`docs/ROADMAP.md`, `CLAUDE.md` §6) | Before | Now |
|---|---|---|---|
| C1 | Every entry carries a state, and one of them is **terminal** | ✗ two evidence marks, no state at all, nothing that can mean *fixed* | ✓ §2, stamped on all 82 |
| C2 | A claimed or finished entry names **who** | ✗ | ✓ on the 8 that changed state |
| C3 | A finished entry names **the PR that finished it** | ✗ | ✓ |
| C4 | The derived summary **agrees with the body**, and a test says so | ~ §3 agreed; §8.1 and §8.5 did not | ✓ `tests/unit/test_audit_summary.py` (#129) |
| C5 | A marker that claims something **outside the document** is checked there | ✗ 82 `file:line` citations, unchecked | ✓ `tests/unit/test_audit_citations.py` (#129) |
| C6 | An entry found wrong is corrected **in the diff that discovers it** | ✗ | ✓ §10 for this file; finding 33's roadmap paragraph in #129 |

### 10.2 C1 — seven findings were fixed and nothing said so

Seven findings were closed by PRs that never mentioned this file, because there
was no state to put a closure in:

| # | Finding | Closed by |
|---:|---|---|---|
| 8 | DASHBOARD.md said login rate limiting was not built | #113 |
| 16 | `login`'s docstring said there was no rate limit | #113 |
| 19 | `unsubscribe` from the last symbol turned the filter into a firehose | #121 |
| 20 | A client dropped on the send deadline was never closed | #120 |
| 35 | `ATP_DB_PASSWORD` had no `.env.example` entry | #113 |
| 38 | compose and the Makefile said the worker cannot place orders | #124 |
| 74 | `NO_RISK_RULES_WARNING` was dead | #111 |

An eighth is closed now — finding **33**, in #129 — and it is the one that did
not happen this way. It was fixed *because* it was recorded here, by a diff that
says so and annotates both the roadmap and this file in the same change. That is
what C6 asks for, and it is the only one of the eight that got it.

Finding **34** is half-closed and is the case that argues for the middle state
existing at all. #124 moved the third live lock out of the environment and into
`worker_config.allow_live_orders`, and added it to SAFETY.md's layered-defences
table as row `2a` — but the go-live checklist in the same file still does not
list it. "Fixed" and "not fixed" are both false; the roadmap has a line shape for
precisely this and now so does this file.

Note what half-closing it also did to the finding's own wording: finding 34
names `WORKER_ALLOW_LIVE_ORDERS`, an environment variable #124 removed. A
finding can be overtaken by its own fix, not just left behind by the code.

### 10.3 C4 — two derived numbers had drifted, in opposite ways

§3's severity and area tables were rebuilt from the 82 entries and **agree
exactly**. Two other derived numbers did not:

- §8.1 said *"60 of the 82 findings are marked ⚠️"*. There are 57. It was wrong
  on the day it was written — 60 + the 25 verified in the same sentence is 85,
  three more findings than the document contains.
- §8.5 said *"Phase 4 is 0/10 on the roadmap"*. True on 2026-08-27; false since
  #127 split the strategy endpoint out of the lifecycle, making it 0/11. Nothing
  could have noticed: it is a number copied out of a file this one has no link
  to.

The first is an arithmetic error a test would have caught. The second is the
failure mode `tests/unit/test_roadmap_summary.py` exists to prevent, one document
over. Both are corrected above; §8.5 now states the fact without the count, so it
cannot drift again.

Three **findings** carry the same defect in their own text — a count that was
right on the day and is wrong now, because the thing counted kept growing:

| # | Said | Says today |
|---:|---|---|---|
| 23 | the screen filters 6 of the **11** actions the platform writes | 6 of **12** — #124 added `worker_config_updated` |
| 66 | DASHBOARD_STATUS.md undercounts the audit verbs by **six** | by **seven**, same cause |
| 82 | `CORE_SUBPACKAGES` omits **four of the fourteen** | **five of fifteen** — #124 added `atp_core.worker` |

All three findings are still open and still right in substance. Each now carries
a record note. A finding that states a count is a derived summary with no test
behind it, which is the whole of C4 restated at the level of one entry.

### 10.4 C5 — 27 of the 82 citations no longer resolve

Every finding's `file:line` is a claim about a tree, in the same way `wip #12` is
a claim about GitHub: true when written, checkable only against something outside
the document, and silently false afterwards. `scripts/check_roadmap_wip.py`
exists because that gap was not hypothetical for the roadmap. It was not
hypothetical here either.

Resolving all 82 citations at `a71ae8f` and again at `4f68cf4`:

- **55** resolve to the same text they did on the day.
- **26** resolve to something else. Some are near-misses (finding 47 cites
  `models.py:225`; the first of the four dead `relationship()` declarations is at
  `:227`). Some are unrecognisable (finding 7 cited `DASHBOARD.md:247` for a
  claim that is at `:808`, and `:247` is now a sentence about round trips).
- **1** never resolved at all: finding 81 cites `tests/e2e/__init__.py:1`, and
  that file has been zero bytes since it was committed. There is no line 1 and
  there never was — the citation was decorative on the day it was written.

Of the 26, **23** have been re-pointed by anchor text to the line that holds the
subject today, each with a record note giving the old line so the finding stays
checkable against `a71ae8f`. Finding 16 drifted because the defect at that line
was fixed. Finding 63's line still holds its subject, shifted. Finding 36 cites
`ci.yml:163` for a CI step that *is not there*, so — like finding 81 — it now
says plainly that it points at an absence rather than a line.

One more citation was re-pointed without having drifted: finding 34's `:23` still
reads what it read, but the row that half-closes it was inserted beneath, at
`:24`.

### 10.5 C6 — the sharpest result, closed in #129

Finding **33** said `docs/ROADMAP.md`'s ticked Phase 2 item still claimed the
backtest risk chain was empty and sizing fixed-qty-only, when both had changed.
The sentence read:

> Sizing is a fixed share count (`--qty`), so the return is a property of that
> share count […] And no pre-trade rule refuses anything: orders are routed
> through `RiskEngine`, but the chain is empty. Both are Phase 3, which the
> build order puts after this.

Every clause of that was false. Sizing had gone through `risk.rules.position_size`
since #81, with `--sizing` and `--sizing-value` on the CLI and `fixed_qty` kept
only as the default. The chain had been `risk.engine.backtest_rules()` since the
same PR — five of the nine, the other four excluded by decision rather than
omission. And neither was Phase 3 work still to come: both were built, and the
Phase 3 boxes were unticked for the *other* reason the conventions table gives,
which is that a phase's *Verifiable:* line has not been shown.

**How long it survived is the finding.** By 2026-09-02, forty-four commits had
edited `docs/ROADMAP.md` since #81, and eighteen since this file named the
sentence — among them #110, #111 and #112, which are the PRs that finished
carrying those caveats into the result the CLI prints. Not one touched the
paragraph that denied the work existed. `tests/unit/test_roadmap_summary.py`
passed on every one of those days, because a stale paragraph inside a ticked
item is not a number, and every assertion that file makes is about numbers.

`CLAUDE.md` §6 says an item found wrong is fixed *in the PR that discovered it*.
The PR that discovered it was #108, which wrote this file, and #128, which
reviewed it, left the correction for a diff that said so. This is that diff: the
paragraph is rewritten to what is true, annotated with the PR that made it true
and the one that corrected the record, and finding 33 is closed.

What remains uncovered is the shape of the failure rather than this instance of
it. Both roadmap tests and both audit tests check that a document agrees with
itself; none of them can read a paragraph of prose inside a ticked item and
notice that it describes a platform that no longer exists. That needs a person,
and what this review shows is that it needs a person who is *required* to look —
which §10.7 is about.

### 10.6 The two checks — added in #129

§10 first said C4 and C5 were met by hand in that diff and by nothing
afterwards, and left both for a diff of their own. This is that diff.

- **C4 — `tests/unit/test_audit_summary.py`.** Every number in the header, §3
  and §8.1 is recomputed from the 82 findings and compared. It also holds the
  two annotation rules §2 states but nothing enforced: a closed or half-closed
  finding must name `— @who (#12)`, and an open one must name nobody, so an
  annotation left behind when a state is walked back cannot go on reading as
  work that was done.

  **It failed on its first run, on this file, for a reason worth recording.**
  §3's glance table carries a *second copy* of each high-severity finding's
  title and location. #128 re-anchored 24 citations in the finding bodies and
  never touched that table, so findings 6, 7 and 8 cited two different lines of
  the same file from two places in the same document — from the moment the
  review that was supposed to fix exactly this merged. The rows are corrected
  here and now carry the finding's state as well, because a reader glancing down
  a list of high-severity defects should not have to scroll to learn that one of
  them is closed.

- **C5 — `tests/unit/test_audit_citations.py`.** Every `file:line` must name a
  file that exists and a line inside it. A unit test rather than a CI gate, and
  that is the whole difference between this and `scripts/check_roadmap_wip.py`:
  a `wip` marker's truth lives on GitHub, so reading it needs a network and a
  check that reddens when the network is down teaches people to re-run it
  (ADR 0024). A citation's target is in the checkout. There is no unreachable
  state, so there is no reason to be lenient — it runs offline on every commit
  and is red when a citation is broken.

  It cannot check that a line still holds what a finding says it holds; that
  needs a person. What it catches is the failure that actually happened
  twenty-six times. Findings 36 and 81 point at a file rather than a line —
  the finding *is* that something is missing from it — and now say so with an
  explicit `**Cites an absence.**` in their record note rather than earning the
  exemption by how their prose happens to read. The test holds that set against
  a literal, so granting a third is a decision somebody makes on purpose.

Neither check knows anything about trading, and both are ninety lines. The cost
of not having had them was six days of a file that read as current, three
citations that disagreed with themselves inside one document, and a §8.1 that
was wrong by three on the day it was written.

### 10.7 What is still missing

There is a plainer gap underneath all six, and #129 only half closes it. Until
that PR, **nothing in this repository referenced this file** — not a test, not a
Makefile target, not a CI job, not `CLAUDE.md`, not another document. Two tests
read it now and `docs/TESTING.md` names them, which is most of the way: `make
check` reaches them through the unit suite on every commit. What is still true
is that no Makefile target is about this file, no CI step names it, and
`CLAUDE.md` §6 — the section that makes the roadmap somebody's job to update —
still points only at `docs/ROADMAP.md`. The roadmap has all four, which is why
its conventions were enforceable enough to audit against.

That asymmetry is defensible — the roadmap is the record the working agreement
is built on and this is a snapshot of one review — but it is worth naming rather
than discovering. A record with no reader is the condition every finding in
§10.2 through §10.5 is a symptom of, and two tests are a reader, not a habit.

---

*Audit produced by Claude Code. Method: repository-wide static review across 13
subsystem dimensions, plus direct execution of the repository's own gates and
targeted probes (route enumeration under a `TestClient`, an AST-based
ORM/migration differ, an unused-symbol scan, and a regeneration diff of the
OpenAPI-derived TypeScript types).*

*§10 added 2026-09-02 by Claude Code. Method: every finding's severity, kind and
evidence mark re-parsed from the body and cross-footed against §3; every
`file:line` citation resolved twice, once against `a71ae8f` and once against
`4f68cf4`, and re-anchored by text where it had moved; every finding whose cited
file changed in #109–#127 re-read against the current source, and the closing
commit identified with `git log -S` on the text that fixed it. §10.6 and the two
checks it describes added in #129, after the first of them failed on §3's glance
table.*
