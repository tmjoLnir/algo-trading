/**
 * The form that queues a run.
 *
 * The only control in this app that starts work, and the only form of any kind.
 * Three things about it are deliberate:
 *
 * - **It offers only strategies a worker has actually run.** A backtest's
 *   `strategy_id` is a foreign key onto `strategies`, a table written by the
 *   runner at its first session open — so a class that exists in the code and has
 *   never been loaded cannot be backtested, and offering it would produce a 409
 *   for a choice this screen invited. The `never_run` names are listed *outside*
 *   the picker instead, with what to do about it.
 * - **Cash and quantity are typed as text, never as numbers.** `<input
 *   type="number">` hands back a JavaScript number, and a starting cash that had
 *   been through IEEE 754 would propagate into every figure the run reports
 *   (CLAUDE.md §1.1). The server takes a decimal string.
 * - **The zero-cost option says what it costs.** docs/BACKTESTING.md is
 *   unambiguous that a zero-cost result is not evidence about a strategy, so the
 *   option is labelled with that rather than reading as the quicker choice.
 *
 * Validation is deliberately thin. The server checks everything — including the
 * one thing this page cannot know, whether the history is stored — and its
 * refusal is shown verbatim, because it names the exact `backfill_bars.py`
 * command that fixes the common case. Duplicating those rules here would give
 * two answers to one question and the client's would be the one that drifts.
 *
 * **There is one deliberate exception, and it is a check on this form rather
 * than a copy of a server rule.** The strategy is the only value here nobody
 * types — it is derived from a list fetched at runtime — which makes it the only
 * one that can be empty without anybody having done anything. The server's
 * refusal for that case is `strategy_id is empty`: correct, and unreadable next
 * to a picker visibly showing a strategy, because it describes the request and
 * the fault is in the row. So a blank id is refused here, beside the control
 * that produced it, and named as what it is.
 */

import { cloneElement, useState } from 'react'
import { ApiError } from '@/api/client'
import {
  ATR_STOP_TYPES,
  COST_MODELS,
  SIZING_METHODS,
  STOP_TYPES,
  TIMEFRAMES,
  useQueueBacktest,
} from '@/hooks/useBacktests'
import type { AvailableStrategyView, StoredStrategyView } from '@/api/types'

interface Props {
  /** Strategies with a `strategies` row — the only ones that can be queued. */
  runnable: StoredStrategyView[]
  /** Registered classes, for the params hint and the never-run notice. */
  available: AvailableStrategyView[]
  neverRun: string[]
  /** False for a read-only session: queueing work is an act (ADR 0009). */
  mayAct: boolean
}

/** A year of daily bars ending today, as `YYYY-MM-DD`. A starting point, not a
 * recommendation — docs/BACKTESTING.md asks for at least two years including a
 * drawdown, which the panel says beneath the form. */
function defaultRange(): { start: string; end: string } {
  const end = new Date()
  const start = new Date(end)
  start.setFullYear(start.getFullYear() - 2)
  return { start: start.toISOString().slice(0, 10), end: end.toISOString().slice(0, 10) }
}

/**
 * One labelled control.
 *
 * The label is associated by `htmlFor`, and the hint is **outside** it, tied on
 * with `aria-describedby`. Wrapping both in one `<label>` — which this did —
 * folds the hint into the field's accessible name, so a screen reader announces
 * "Sizing how a quantity is decided" as the name of the control rather than as
 * a description of it. The distinction is also what lets a test ask for a field
 * by the name a person would call it.
 */
function Field({
  label,
  hint,
  children,
}: {
  label: string
  hint?: string
  children: React.ReactElement<{ id?: string; 'aria-describedby'?: string }>
}) {
  const id = label.toLowerCase().replace(/[^a-z0-9]+/g, '-')
  const hintId = hint ? `${id}-hint` : undefined
  return (
    <div className="flex flex-col gap-1 text-xs">
      <label htmlFor={id} className="font-medium text-slate-400">
        {label}
      </label>
      {cloneElement(children, { id, 'aria-describedby': hintId })}
      {hint ? (
        <span id={hintId} className="text-slate-600">
          {hint}
        </span>
      ) : null}
    </div>
  )
}

const INPUT =
  'rounded border border-slate-700 bg-slate-950 px-2 py-1.5 text-sm text-slate-100 ' +
  'placeholder:text-slate-600 disabled:opacity-50'

export default function BacktestForm({ runnable, available, neverRun, mayAct }: Props) {
  const range = defaultRange()
  const queue = useQueueBacktest()

  // The *chosen* strategy, which is empty until somebody chooses one — not the
  // strategy this form will queue. That distinction is the bug this shape fixes:
  // `useState(runnable[0]?.id ?? '')` read the list at mount, and this form
  // mounts on the page's first render, before the strategies query has resolved
  // and while `runnable` is still `[]`. A state initialiser does not re-run when
  // the data lands, so the value stayed `''` for the life of the form — and
  // React, whose controlled `<select>` selects the first option when the value
  // matches none of them, displayed a strategy that was not the one being sent.
  // The button posted an empty `strategy_id` and the API answered `unknown
  // strategy ''`, which reads as a registry fault and is a form fault.
  //
  // Derived at render rather than synced in an effect, deliberately: an effect
  // would queue a second render in which the form still holds the stale value,
  // and anything reading it in between — a submit — sees the wrong answer. This
  // cannot be stale, because it is recomputed from the list it depends on.
  const [chosenStrategyId, setChosenStrategyId] = useState('')
  // The fallback applies only while the choice is not one of the offered rows,
  // which covers both "nothing chosen yet" and a strategy that has left the
  // list. An explicit, still-valid choice always wins — the strategies query
  // refetches on window focus, and a fallback that reasserted itself there would
  // re-point the run at a strategy nobody picked.
  const strategyId = runnable.some((strategy) => strategy.id === chosenStrategyId)
    ? chosenStrategyId
    : (runnable[0]?.id ?? '')
  // What actually gets posted, and the reason it is not just `strategyId`.
  //
  // **Trimmed, because the server trims.** `POST /backtests` strips the name
  // before both the registry lookup and the spec it stores, so an id arriving
  // with whitespace around it is accepted there and then fails the foreign key
  // onto `strategies.id` — surfacing as a 409 about a strategy no worker has
  // run, which is a sentence about the wrong thing. Sending what the server will
  // use keeps the two ends agreeing. `symbols` two lines below has always been
  // trimmed here for the same reason.
  //
  // **Checked for blank, because this is the one field on the form nobody
  // types.** Every other value is either typed or picked from a constant; this
  // one is derived from a list fetched at runtime, which makes it the only one
  // that can silently be empty. A `strategies` row carries whatever
  // `Strategy.name` the worker booted with, so a blank or whitespace-only name
  // makes a row that shows a label in the picker and carries no id — the exact
  // shape of "the strategy is right there and the server says it is empty".
  const queueableStrategyId = strategyId.trim()
  const [symbols, setSymbols] = useState('')
  const [start, setStart] = useState(range.start)
  const [end, setEnd] = useState(range.end)
  const [timeframe, setTimeframe] = useState('1d')
  const [cash, setCash] = useState('100000')
  const [qty, setQty] = useState('100')
  const [sizingMethod, setSizingMethod] = useState('fixed_qty')
  // Held separately from `qty` so switching method does not silently reinterpret
  // a share count as a fraction of equity — 100 shares and 100× the account are
  // the same three characters.
  const [sizingValue, setSizingValue] = useState('')
  const [stopType, setStopType] = useState('')
  const [stopValue, setStopValue] = useState('')
  const [stopPeriod, setStopPeriod] = useState('14')
  const [costModel, setCostModel] = useState<string>(COST_MODELS[0].value)

  // The picked strategy's own declaration of what it takes. Shown rather than
  // rendered as a form: nothing here builds inputs from a JSON Schema, and
  // pretending to would mean silently dropping the fields it could not handle.
  const picked = available.find((entry) => entry.name === strategyId)
  const sizingUnit = SIZING_METHODS.find((option) => option.value === sizingMethod)?.unit ?? 'value'
  const stopUnit = STOP_TYPES.find((option) => option.value === stopType)?.unit ?? 'value'
  // A time stop counts bars rather than measuring a distance, so its number
  // goes to `stop_bars` and `stop_value` stays empty.
  const stopIsTime = stopType === 'time'
  const paramsSchema = picked?.params_schema ?? {}

  const submit = (event: React.FormEvent) => {
    event.preventDefault()
    queue.mutate({
      strategy_id: queueableStrategyId,
      symbols: symbols
        .split(',')
        .map((symbol) => symbol.trim().toUpperCase())
        .filter(Boolean),
      // Midnight UTC, explicitly. A date input gives a bare `YYYY-MM-DD`, and
      // the server refuses a naive datetime at the boundary (CLAUDE.md §1.2) —
      // so the zone is added here rather than left for the server to assume.
      start: `${start}T00:00:00Z`,
      end: `${end}T00:00:00Z`,
      timeframe,
      // Strings. See the module docstring — this is why the inputs are text.
      starting_cash: cash,
      qty,
      sizing_method: sizingMethod,
      // Omitted rather than empty: the server reads "not given" as "use qty",
      // which is what keeps a fixed_qty request identical to what it always was.
      sizing_value: sizingValue.trim() || null,
      stop_type: stopType,
      // A time stop counts bars rather than measuring a distance, so its number
      // goes to `stop_bars` and `stop_value` stays empty — the server refuses a
      // time stop with no bar count rather than defaulting one.
      stop_value: stopIsTime ? null : stopValue.trim() || null,
      stop_bars: stopIsTime ? Number(stopValue.trim() || 0) : 0,
      stop_period: Number(stopPeriod.trim() || 14),
      cost_model: costModel,
      params: {},
    })
  }

  if (runnable.length === 0) {
    return (
      <div className="rounded border border-slate-800 bg-slate-900/20 px-4 py-6 text-sm text-slate-400">
        <p>No strategy can be backtested yet.</p>
        <p className="mt-1 text-xs text-slate-500">
          A run is recorded against a row in <code className="text-slate-400">strategies</code>,
          which a worker writes the first time it loads a strategy.{' '}
          {neverRun.length > 0 ? (
            <>
              <span className="text-amber-300">{neverRun.join(', ')}</span>{' '}
              {neverRun.length === 1 ? 'exists' : 'exist'} in the code and{' '}
              {neverRun.length === 1 ? 'has' : 'have'} never run — set{' '}
              <code className="text-slate-400">WORKER_STRATEGY</code> and start the worker once.
            </>
          ) : (
            'Start a worker with WORKER_STRATEGY set.'
          )}
        </p>
      </div>
    )
  }

  return (
    <form onSubmit={submit} className="rounded border border-slate-800 bg-slate-900/20 p-4">
      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
        <Field label="Strategy" hint={picked ? picked.class_name : undefined}>
          <select
            value={strategyId}
            onChange={(event) => setChosenStrategyId(event.target.value)}
            disabled={!mayAct}
            className={INPUT}
          >
            {runnable.map((strategy) => (
              <option key={strategy.id} value={strategy.id}>
                {strategy.name}
              </option>
            ))}
          </select>
        </Field>

        <Field label="Symbols" hint="comma separated, e.g. SPY,QQQ">
          <input
            value={symbols}
            onChange={(event) => setSymbols(event.target.value)}
            placeholder="SPY"
            disabled={!mayAct}
            className={INPUT}
          />
        </Field>

        <Field label="From">
          <input
            type="date"
            value={start}
            onChange={(event) => setStart(event.target.value)}
            disabled={!mayAct}
            className={INPUT}
          />
        </Field>

        <Field label="To">
          <input
            type="date"
            value={end}
            onChange={(event) => setEnd(event.target.value)}
            disabled={!mayAct}
            className={INPUT}
          />
        </Field>

        <Field label="Timeframe">
          <select
            value={timeframe}
            onChange={(event) => setTimeframe(event.target.value)}
            disabled={!mayAct}
            className={INPUT}
          >
            {TIMEFRAMES.map((option) => (
              <option key={option} value={option}>
                {option}
              </option>
            ))}
          </select>
        </Field>

        {/* Text, not `type="number"`. See the module docstring. */}
        <Field label="Starting cash" hint="exact — sent as a decimal string">
          <input
            value={cash}
            onChange={(event) => setCash(event.target.value)}
            inputMode="decimal"
            disabled={!mayAct}
            className={INPUT}
          />
        </Field>

        <Field label="Sizing" hint="how a quantity is decided">
          <select
            value={sizingMethod}
            onChange={(event) => {
              setSizingMethod(event.target.value)
              setSizingValue('')
            }}
            disabled={!mayAct}
            className={INPUT}
          >
            {SIZING_METHODS.map((option) => (
              <option key={option.value} value={option.value}>
                {option.label}
              </option>
            ))}
          </select>
        </Field>

        {sizingMethod === 'fixed_qty' ? (
          <Field
            label="Shares per entry"
            hint="a constant — the return is a property of this number too"
          >
            <input
              value={qty}
              onChange={(event) => setQty(event.target.value)}
              inputMode="decimal"
              disabled={!mayAct}
              className={INPUT}
            />
          </Field>
        ) : (
          <Field label="Sizing value" hint={sizingUnit}>
            <input
              value={sizingValue}
              onChange={(event) => setSizingValue(event.target.value)}
              inputMode="decimal"
              placeholder={sizingUnit}
              disabled={!mayAct}
              className={INPUT}
            />
          </Field>
        )}

        <Field label="Stop" hint="how every entry is protected">
          <select
            value={stopType}
            onChange={(event) => {
              setStopType(event.target.value)
              setStopValue('')
            }}
            disabled={!mayAct}
            className={INPUT}
          >
            {STOP_TYPES.map((option) => (
              <option key={option.value} value={option.value}>
                {option.label}
              </option>
            ))}
          </select>
        </Field>

        {stopType ? (
          <Field label="Stop value" hint={stopUnit}>
            <input
              value={stopValue}
              onChange={(event) => setStopValue(event.target.value)}
              inputMode="decimal"
              placeholder={stopUnit}
              disabled={!mayAct}
              className={INPUT}
            />
          </Field>
        ) : null}

        {ATR_STOP_TYPES.has(stopType) ? (
          <Field label="ATR period" hint="lookback the multiple is measured against">
            <input
              value={stopPeriod}
              onChange={(event) => setStopPeriod(event.target.value)}
              inputMode="numeric"
              disabled={!mayAct}
              className={INPUT}
            />
          </Field>
        ) : null}

        <Field label="Cost model">
          <select
            value={costModel}
            onChange={(event) => setCostModel(event.target.value)}
            disabled={!mayAct}
            className={INPUT}
          >
            {COST_MODELS.map((option) => (
              <option key={option.value} value={option.value}>
                {option.label}
              </option>
            ))}
          </select>
        </Field>
      </div>

      {Object.keys(paramsSchema).length > 0 ? (
        <p className="mt-3 text-xs text-slate-500">
          {strategyId} runs with its configured parameters. Editing them per run is not built —
          there is no form for a JSON Schema, and one that silently dropped the fields it could not
          render would report a result for parameters nobody chose.
        </p>
      ) : null}

      <div className="mt-4 flex flex-wrap items-center gap-3">
        <button
          type="submit"
          disabled={!mayAct || queue.isPending || !symbols.trim() || !queueableStrategyId}
          className="rounded bg-sky-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-sky-500 disabled:opacity-40"
        >
          {queue.isPending ? 'Queueing…' : 'Queue backtest'}
        </button>

        {!mayAct ? (
          // Said rather than left to be discovered by a 403. Running a backtest
          // occupies the shared queue for minutes, which is an act.
          <span className="text-xs text-slate-500">
            This session is read-only. You can read every run below and compare them; queueing one
            is an act.
          </span>
        ) : !queueableStrategyId ? (
          // A picker with rows in it and no id to send. Named here rather than
          // left to the server, because the server can only say `strategy_id is
          // empty` — true, and unreadable next to a picker visibly showing a
          // strategy. The fault is in the row, and this says which row and what
          // is wrong with it.
          <span className="text-xs text-amber-300">
            The selected strategy has a blank id, so there is nothing to queue. Its{' '}
            <code className="text-amber-200">strategies</code> row was written from a strategy whose
            name is empty — check the worker that created it.
          </span>
        ) : null}

        {queue.isError ? (
          // Verbatim, and this is the important half of the form. The server's
          // refusal names the exact backfill command for missing history, which
          // is the most likely reason a request is rejected — paraphrasing it
          // would turn an actionable message into a dead end.
          <span className="text-xs text-amber-300">
            {queue.error instanceof ApiError ? queue.error.detail : String(queue.error)}
          </span>
        ) : null}

        {queue.isSuccess && !queue.isError ? (
          <span className="text-xs text-emerald-400">
            Queued. It appears below and updates as it runs.
          </span>
        ) : null}
      </div>

      <p className="mt-3 text-xs text-slate-600">
        Before believing a result: at least two years including a drawdown, 100+ trades, realistic
        costs, and individual trades inspected for impossible fills. The full checklist is in
        docs/BACKTESTING.md.
      </p>
    </form>
  )
}
