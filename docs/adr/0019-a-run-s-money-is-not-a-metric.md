# 19. A run's money is not a metric

**Status:** Accepted · 2026-08-27

## Context

`BacktestResult.to_report()` produces nine figures that describe the money a run
made and the orders it took: `ending_equity`, `total_return` as a decimal
string, `realized_pnl`, `unrealized_pnl`, `open_positions`, `orders`,
`filled_orders`, `signals`, `fees`. The CLI's `--out` JSON has all nine.

A **queued** run had none of them, at any layer. `runner.result_to_storage`
returned metrics, the equity curve, the trades and the warnings;
`BacktestRunRow` had no column for a money field; and `BacktestOut` exposed
`metrics: dict[str, float | None]` and nothing else numeric. The engine computed
all nine on every queued run and threw them away.

The gap came from two decisions that are each correct:

- `metrics` is a bag of **floats** by contract, and it has to be — these are
  statistics over a return series, not balances. Money must not be float
  (`CLAUDE.md` §1.1), so `to_report()` deliberately keeps the money fields *out*
  of `metrics` and reports them as decimal strings beside it.
- The queued path stored **only** `metrics`, the curve and the trades.

Together they dropped every money figure a run produced.

What that cost is not abstract. A `buy_and_hold` run over twenty symbols
reported a total return of **202.8%**, of which **none was realised**:
`realized_pnl` zero, the whole 202.8% unrealised mark-to-market, twenty
positions still open at the end. On the dashboard it read "202.8% return" with
nothing to say otherwise. `to_report()`'s own docstring names the hazard:

> `realized_pnl` and `unrealized_pnl` are reported separately because
> `ending_equity` alone is readable two ways and only one of them is a track
> record.

The persisted path kept *neither* reading. The nearest hint was `num_trades: 0`
in the metric set, which says something different — that the trade statistics
rest on no closed round trips — and reads as "this strategy does not trade much"
rather than "you are still holding all of it". It bites hardest on exactly the
strategies a reader is most likely to misjudge.

## Decision

**The money is stored, in a column of its own, as decimal strings — never
folded into `metrics`.**

`backtest_runs.totals` is one additive nullable JSON column holding all nine:
money as decimal strings, the four counts as integers. `BacktestResult.totals()`
is the single assembly; `to_report()` spreads it, so the CLI's report and the
stored row cannot disagree about what one backtest made (ADR 0006's rule, and
the reason `build_engine` exists).

**One column rather than nine.** Five of the nine are money and four are
counts, so "a set of `MONEY` columns" would be nine columns of two types on a
table that already carries a run's entire result as JSON. `metrics`,
`equity_curve`, `trades` and `warnings` are written in one transaction
precisely so a `done` row cannot carry half a result; a fifth of the same kind
keeps that invariant, where nine scalar columns would spread it across a wider
surface for no reader's benefit. `equity_curve` is the standing precedent for
money as decimal strings inside JSON, and it is the same money.

**`metrics` does not gain them.** A P&L that round-trips through a JSON number
is no longer exact, and the whole point of `Decimal` server-side is that it
never does (`CLAUDE.md` §1.1). `total_return` therefore exists twice over — a
float in `metrics` and a decimal string in `totals` — computed from one equity
by one engine. That is not duplication to be resolved: it is one quantity in the
two types the two readers need, and the type is what says which formatter may
touch it. `stats.ts` renders the statistic; `money.ts`, which accepts only
strings, renders the ledger figure.

**Nullable with no backfill.** A run stored before the column computed these and
discarded them. NULL says that; zeros would be figures nobody can check. Same
decision as `a9f37c14e6b2` made for `warnings`, and the screen states it in
words rather than rendering a nought.

**The run panel says the sentence.** A "Money" block above "Metrics", and where
a run ends still holding: *"N positions still open at the end, carrying X of
unrealised mark-to-market. That is part of the return above and part of none of
the trade statistics below, which count closed round trips only."* The wording
is `scripts/run_backtest.py`'s, unchanged — the CLI has printed it all along, and
two wordings of one caveat is how they drift.

## Consequences

- A queued run and a CLI run report the same nine figures, from one assembly.
- A return that is entirely a mark says so on the screen, above the statistics
  whose meaning it changes.
- `finish()` takes a fifth argument. Every caller had to move, which is a real
  cost and the one CI caught in #104; `mypy libs apps tests` caught all nineteen
  call sites this time, six of them in integration tests that do not run
  locally.
- Old rows read `null` and the screen explains itself rather than showing zeros.
  That branch is permanent — there is no backfill that would ever remove it.
- The API validates `totals` against a fixed model rather than passing the bag
  through as `metrics` is. There is one writer, so a row that does not fit the
  model is a disagreement between the writer and the schema and should be loud.

## Alternatives considered

**Nine typed columns** (`MONEY` for the five, `INTEGER` for the four). Queryable
— "runs whose ending equity beat X" — which nothing does today and nothing is
planned to. It spreads one transactional result across nine columns and makes
every future figure another migration. Revisit if a query over these ever has a
caller.

**Fold them into `metrics` and accept floats.** Rejected by `CLAUDE.md` §1.1
without further argument. It is the exact bug the rule exists to prevent, on the
values it exists to protect.

**Derive them on read from `metrics`.** Impossible, which is the whole point:
`realized_pnl` is not a function of the metric set. A run that ended holding
everything and one that banked the same return have identical metrics.
`suspicious()` already showed the limit of that approach for `warnings` (#104),
and this is the same wall.
