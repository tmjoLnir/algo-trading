/**
 * What the worker trades, edited here rather than in a file on the host.
 *
 * These ten values were environment variables. Moving them onto a screen is
 * mostly about reach — changing a stop multiplier no longer needs SSH — but the
 * part that shapes this component is the part that does not go away: **a worker
 * reads its configuration once, at start.** So there are always two answers to
 * "what is this platform trading", and a form that showed only the one it can
 * edit would be quietly wrong for the whole gap between a save and a restart.
 *
 * Hence the three states this renders, which must not look alike:
 *
 * 1. **In force.** A worker has reported, and its revision is the saved one.
 * 2. **Saved, not running.** A worker has reported an older revision. The form
 *    is the future; the summary above it is the present; the banner says which
 *    is which and what to do about it.
 * 3. **Nobody has reported.** No worker has published — never started, or
 *    Redis was flushed. Not the same as "running nothing": the screen says the
 *    configuration is stored and unobserved, rather than implying it is live.
 *
 * **The one field that asks for a password.** `allow_live_orders` is the third
 * of the three live-money locks. Arming it here reveals a password box and the
 * server checks it (ADR 0009 — a cookie proves somebody signed in this morning,
 * not that anybody is at the keyboard now). Turning it off asks for nothing, in
 * the same asymmetry the halt and resume buttons have: stopping must never be
 * the harder direction.
 *
 * **The multiplier's label changes with the stop type**, from the server's own
 * `multiplier_stops` list rather than a copy of it here. An ATR stop reads that
 * number as a multiple and a fixed-percentage stop reads it as a fraction; one
 * field carries both, and an input labelled "multiplier" next to a `fixed_pct`
 * stop is how somebody types 2 and gets a stop 200% below entry.
 */

import { useEffect, useMemo, useState } from 'react'
import { ApiError } from '@/api/client'
import { useSaveWorkerConfig, useWorkerConfig } from '@/hooks/useWorkerConfig'
import { formatDateTime } from '@/lib/money'
import type {
  RunningConfigView,
  StrategyOptionView,
  WorkerConfigScreen,
  WorkerConfigView,
  WorkerOption,
} from '@/api/types'

/** The form's own state: every value a string, because every input is one. */
interface Draft {
  symbols: string
  maxSilenceSeconds: string
  strategy: string
  strategyParams: string
  sizingMethod: string
  sizingValue: string
  stopType: string
  stopMultiplier: string
  stopPeriod: string
  allowLiveOrders: boolean
}

function toDraft(config: WorkerConfigView): Draft {
  return {
    symbols: config.symbols.join(', '),
    maxSilenceSeconds: String(config.max_silence_seconds),
    strategy: config.strategy,
    // Pretty-printed, and `{}` rendered as an empty box: an operator who has
    // set no parameters should see nothing to delete, not two braces to work
    // around.
    strategyParams: Object.keys(config.strategy_params).length
      ? JSON.stringify(config.strategy_params, null, 2)
      : '',
    sizingMethod: config.sizing_method,
    sizingValue: config.sizing_value,
    stopType: config.stop_type,
    stopMultiplier: config.stop_multiplier,
    stopPeriod: String(config.stop_period),
    allowLiveOrders: config.allow_live_orders,
  }
}

const FIELD =
  'w-full rounded border border-slate-700 bg-slate-900 px-2 py-1 text-sm text-slate-100 ' +
  'placeholder:text-slate-600 focus:border-sky-500 focus:outline-none disabled:opacity-50'

/**
 * One labelled control.
 *
 * `htmlFor` rather than wrapping the input in the `<label>`: the hint sits
 * inside the same block, and a wrapping label would fold it into the control's
 * accessible name — so a screen reader would announce "Watchlist comma
 * separated tickers empty means this worker ingests no market data" as the name
 * of a text box. The hint is described-by instead, which is what it is.
 */
function Field({
  id,
  label,
  hint,
  children,
}: {
  id: string
  label: string
  hint?: string
  children: React.ReactNode
}) {
  return (
    <div className="block">
      <label htmlFor={id} className="text-xs font-medium text-slate-300">
        {label}
      </label>
      {children}
      {hint ? (
        <span id={`${id}-hint`} className="mt-1 block text-[11px] text-slate-500">
          {hint}
        </span>
      ) : null}
    </div>
  )
}

/** The banner that says whether what is on this form is what is running. */
function RunningState({
  running,
  pending,
  savedRevision,
}: {
  running: RunningConfigView | null
  pending: boolean
  savedRevision: number
}) {
  if (running === null) {
    return (
      <div className="rounded border border-slate-700 bg-slate-900/60 px-3 py-2 text-xs text-slate-400">
        <span className="font-semibold text-slate-300">No worker has reported.</span> Nothing has
        published what it booted with — either none has started, or the store was cleared. What is
        saved below is stored and unobserved; it is not evidence that anything is running it.
      </div>
    )
  }
  if (pending) {
    return (
      <div
        role="status"
        className="rounded border border-amber-700 bg-amber-950/50 px-3 py-2 text-xs text-amber-200"
      >
        <span className="font-semibold">Saved, not running.</span> The worker started{' '}
        {formatDateTime(running.started_at)} on revision {running.revision}; revision{' '}
        {savedRevision} is saved. Configuration is read once at start —{' '}
        <code className="text-amber-100">docker compose restart worker</code> to apply it.
      </div>
    )
  }
  return (
    <div className="rounded border border-emerald-800 bg-emerald-950/40 px-3 py-2 text-xs text-emerald-200">
      <span className="font-semibold">In force.</span> The worker started{' '}
      {formatDateTime(running.started_at)} on revision {running.revision}, which is what is saved.
    </div>
  )
}

/** What the running worker decided, in its own words. */
function RunningSummary({ running }: { running: RunningConfigView }) {
  return (
    <p className={`text-xs ${running.trading ? 'text-slate-300' : 'text-slate-500'}`}>
      <span className="font-medium">{running.trading ? 'Trading:' : 'Not trading:'}</span>{' '}
      {running.reason}
    </p>
  )
}

function Select({
  value,
  onChange,
  options,
  emptyLabel,
  id,
}: {
  value: string
  onChange: (value: string) => void
  options: readonly WorkerOption[]
  emptyLabel?: string
  id: string
}) {
  return (
    <select
      id={id}
      value={value}
      onChange={(event) => onChange(event.target.value)}
      className={`${FIELD} mt-1`}
    >
      {emptyLabel ? <option value="">{emptyLabel}</option> : null}
      {options.map((option) => (
        <option key={option.value} value={option.value}>
          {option.label}
        </option>
      ))}
    </select>
  )
}

/** The prose for whatever is currently selected. */
function Help({ options, value }: { options: readonly WorkerOption[]; value: string }) {
  const chosen = options.find((option) => option.value === value)
  if (!chosen) return null
  return <span className="mt-1 block text-[11px] text-slate-500">{chosen.help}</span>
}

export default function WorkerConfigPanel() {
  const query = useWorkerConfig()
  const save = useSaveWorkerConfig()
  const screen: WorkerConfigScreen | undefined = query.data

  const [draft, setDraft] = useState<Draft | null>(null)
  const [password, setPassword] = useState('')
  // A refusal this component found itself, before anything was sent. Shown
  // in the same place as the server's, because to the reader they are the
  // same event: the save did not happen and here is why.
  const [localError, setLocalError] = useState<string | null>(null)

  // The form is seeded from the server and then owned by the operator. Keyed on
  // the revision rather than on the payload: re-seeding on every refetch would
  // discard half-typed edits every time the tab regained focus, and the
  // revision is exactly "the saved configuration became a different one".
  const revision = screen?.saved.revision
  useEffect(() => {
    if (!screen) return
    setDraft(toDraft(screen.saved.config))
    // Dropped whenever the saved configuration changes, which includes the
    // save this password authorised. It is never held longer than the act it
    // travelled with.
    setPassword('')
    setLocalError(null)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [revision])

  const armingLive = Boolean(
    draft?.allowLiveOrders && screen && !screen.saved.config.allow_live_orders,
  )

  const strategyHelp: StrategyOptionView | undefined = useMemo(
    () => screen?.options.strategies.find((s) => s.value === draft?.strategy),
    [screen, draft?.strategy],
  )

  if (query.isPending) {
    return <p className="text-sm text-slate-400">Loading the worker configuration…</p>
  }
  if (query.error || !screen || !draft) {
    const detail =
      query.error instanceof ApiError ? query.error.detail : String(query.error ?? 'unknown')
    return (
      <p role="alert" className="text-sm text-rose-300">
        Could not read the worker configuration: {detail}
      </p>
    )
  }

  const set = <K extends keyof Draft>(key: K, value: Draft[K]) =>
    setDraft((current) => (current === null ? current : { ...current, [key]: value }))

  // One message, whichever end refused. `localError` wins because it is the
  // more recent event: a client-side refusal happens after any earlier failed
  // save and means nothing was sent this time.
  const failure =
    localError ??
    (save.error === null
      ? null
      : save.error instanceof ApiError
        ? save.error.detail
        : String(save.error))

  const usesMultiplier = screen.options.multiplier_stops.includes(draft.stopType)
  const usesPeriod = screen.options.period_stops.includes(draft.stopType)

  const submit = (event: React.FormEvent) => {
    event.preventDefault()
    let params: Record<string, unknown>
    try {
      // Parsed here as well as on the server, because the server's refusal
      // costs a round trip and this one can name the box. The server still
      // refuses it too — this is a convenience, never the check.
      params = draft.strategyParams.trim() === '' ? {} : JSON.parse(draft.strategyParams)
    } catch (error) {
      save.reset()
      setLocalError(`Strategy parameters are not valid JSON: ${String(error)}`)
      return
    }
    setLocalError(null)
    save.mutate({
      symbols: draft.symbols
        .split(',')
        .map((s) => s.trim().toUpperCase())
        .filter(Boolean),
      max_silence_seconds: Number(draft.maxSilenceSeconds),
      strategy: draft.strategy,
      strategy_params: params,
      sizing_method: draft.sizingMethod,
      // Sent as typed. Never through `Number` — these two scale money, and the
      // server holds them as Decimals (src/lib/money.ts).
      sizing_value: draft.sizingValue.trim(),
      stop_type: draft.stopType,
      stop_multiplier: draft.stopMultiplier.trim(),
      stop_period: Number(draft.stopPeriod),
      allow_live_orders: draft.allowLiveOrders,
      ...(armingLive ? { password } : {}),
    })
  }

  return (
    <section className="space-y-4">
      <header className="space-y-2">
        <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
          <h2 className="text-sm font-semibold text-slate-100">Worker</h2>
          <span className="text-xs text-slate-500">
            revision {screen.saved.revision === 0 ? '— never saved' : screen.saved.revision}
            {screen.saved.updated_by
              ? ` · saved by ${screen.saved.updated_by} ${formatDateTime(screen.saved.updated_at)}`
              : ''}
          </span>
          <span className="ml-auto text-xs text-slate-500">
            run mode <span className="text-slate-300">{screen.run_mode}</span>
          </span>
        </div>
        <RunningState
          running={screen.running}
          pending={screen.pending_restart}
          savedRevision={screen.saved.revision}
        />
        {screen.running ? <RunningSummary running={screen.running} /> : null}
      </header>

      <form onSubmit={submit} className="grid gap-4 md:grid-cols-2">
        <div className="md:col-span-2">
          <Field
            id="worker-symbols"
            label="Watchlist"
            hint="Comma-separated tickers. Empty means this worker ingests no market data and places no orders."
          >
            <input
              id="worker-symbols"
              value={draft.symbols}
              onChange={(event) => set('symbols', event.target.value)}
              placeholder="SPY, QQQ"
              className={`${FIELD} mt-1 font-mono`}
            />
          </Field>
        </div>

        <div>
          <label htmlFor="worker-strategy" className="text-xs font-medium text-slate-300">
            Strategy
          </label>
          <Select
            id="worker-strategy"
            value={draft.strategy}
            onChange={(value) => {
              set('strategy', value)
              // Params belong to a strategy. Carrying one strategy's over to
              // another would be refused at construction if you were lucky and
              // silently ignored if you were not.
              set('strategyParams', '')
            }}
            options={screen.options.strategies}
            emptyLabel="— none: place no orders —"
          />
          <Help options={screen.options.strategies} value={draft.strategy} />
          {draft.strategy === '' ? (
            <span className="mt-1 block text-[11px] text-slate-500">
              The worker still ingests market data and runs its schedule; it simply decides nothing.
            </span>
          ) : null}
        </div>

        <div>
          <Field
            id="worker-strategy-params"
            label="Strategy parameters (JSON)"
            hint={
              strategyHelp && Object.keys(strategyHelp.default_params).length
                ? `Empty uses the strategy's defaults: ${JSON.stringify(strategyHelp.default_params)}`
                : "Empty uses the strategy's own defaults."
            }
          >
            <textarea
              id="worker-strategy-params"
              value={draft.strategyParams}
              onChange={(event) => set('strategyParams', event.target.value)}
              rows={3}
              spellCheck={false}
              placeholder={'{"fast_period": 20, "slow_period": 50}'}
              className={`${FIELD} mt-1 font-mono`}
            />
          </Field>
        </div>

        <div>
          <label htmlFor="worker-sizing-method" className="text-xs font-medium text-slate-300">
            Position sizing
          </label>
          <Select
            id="worker-sizing-method"
            value={draft.sizingMethod}
            onChange={(value) => set('sizingMethod', value)}
            options={screen.options.sizing_methods}
          />
          <Help options={screen.options.sizing_methods} value={draft.sizingMethod} />
        </div>

        <div>
          <Field
            id="worker-sizing-value"
            label="Sizing value"
            hint={
              ['risk_pct', 'equity_pct', 'volatility_target'].includes(draft.sizingMethod)
                ? 'A fraction of equity — 0.01 is 1%. Refused above 0.10.'
                : draft.sizingMethod === 'fixed_qty'
                  ? 'A share count.'
                  : 'An amount in the quote currency.'
            }
          >
            <input
              id="worker-sizing-value"
              value={draft.sizingValue}
              onChange={(event) => set('sizingValue', event.target.value)}
              inputMode="decimal"
              className={`${FIELD} mt-1 tabular-nums`}
            />
          </Field>
        </div>

        <div>
          <label htmlFor="worker-stop-type" className="text-xs font-medium text-slate-300">
            Protective stop
          </label>
          <Select
            id="worker-stop-type"
            value={draft.stopType}
            onChange={(value) => set('stopType', value)}
            options={screen.options.stop_types}
          />
          <Help options={screen.options.stop_types} value={draft.stopType} />
        </div>

        <div className="grid grid-cols-2 gap-3">
          <Field
            id="worker-stop-multiplier"
            label={usesMultiplier ? 'Stop multiple' : 'Stop distance'}
            hint={
              usesMultiplier
                ? 'How many ATRs from the reference price.'
                : draft.stopType === 'fixed_amount'
                  ? 'An amount in the quote currency.'
                  : draft.stopType === 'time'
                    ? 'Unused by a time stop.'
                    : 'A fraction — 0.02 is 2%. Must be below 1.'
            }
          >
            <input
              id="worker-stop-multiplier"
              value={draft.stopMultiplier}
              onChange={(event) => set('stopMultiplier', event.target.value)}
              inputMode="decimal"
              disabled={draft.stopType === 'time'}
              className={`${FIELD} mt-1 tabular-nums`}
            />
          </Field>
          <Field
            id="worker-stop-period"
            label={draft.stopType === 'time' ? 'Bars held' : 'Lookback period'}
            hint={usesPeriod ? 'In bars.' : 'Unused by this stop type.'}
          >
            <input
              id="worker-stop-period"
              value={draft.stopPeriod}
              onChange={(event) => set('stopPeriod', event.target.value)}
              inputMode="numeric"
              disabled={!usesPeriod}
              className={`${FIELD} mt-1 tabular-nums`}
            />
          </Field>
        </div>

        <div>
          <Field
            id="worker-max-silence"
            label="Feed silence before halting (seconds)"
            hint="How long the market-data feed may deliver nothing during a session before the watchdog halts trading. Looser than the quote-age limit on purpose — a symbol can legitimately go a minute without printing."
          >
            <input
              id="worker-max-silence"
              value={draft.maxSilenceSeconds}
              onChange={(event) => set('maxSilenceSeconds', event.target.value)}
              inputMode="numeric"
              className={`${FIELD} mt-1 tabular-nums`}
            />
          </Field>
        </div>

        <div className="md:col-span-2 rounded border border-rose-900/70 bg-rose-950/30 p-3">
          <label className="flex items-start gap-2">
            <input
              id="worker-allow-live"
              type="checkbox"
              checked={draft.allowLiveOrders}
              onChange={(event) => {
                set('allowLiveOrders', event.target.checked)
                if (!event.target.checked) setPassword('')
              }}
              className="mt-0.5"
            />
            <span className="text-xs">
              <span className="font-semibold text-rose-200">
                Permit this worker to place live orders
              </span>
              <span className="mt-1 block text-rose-300/80">
                The third of three locks. <code>ATP_RUN_MODE=live</code> and{' '}
                <code>ATP_ALLOW_LIVE_TRADING</code> — both host configuration, not editable here —
                say this process may trade real money; this says this unattended loop may place the
                orders.{' '}
                {screen.run_mode === 'live'
                  ? 'This platform is in live mode: unchecked, no real order is placed.'
                  : `This platform is in ${screen.run_mode} mode, which ignores this setting. It takes effect only if the run mode is changed to live.`}
              </span>
            </span>
          </label>

          {armingLive ? (
            <div className="mt-3 border-t border-rose-900/70 pt-3">
              <label htmlFor="worker-live-password" className="text-xs text-rose-200">
                Your account password, to arm live order placement
              </label>
              <input
                id="worker-live-password"
                type="password"
                autoComplete="current-password"
                value={password}
                onChange={(event) => setPassword(event.target.value)}
                placeholder="account password"
                className="mt-1 w-64 rounded border border-rose-700 bg-rose-950 px-2 py-1 text-sm text-rose-100 placeholder:text-rose-400/60"
              />
              <p className="mt-1 text-[11px] text-rose-300/80">
                Asked because a session cookie proves somebody signed in, not that anybody is at the
                keyboard now. Turning this off never asks.
              </p>
            </div>
          ) : null}
        </div>

        <div className="md:col-span-2 flex flex-wrap items-center gap-3">
          <button
            type="submit"
            disabled={save.isPending || (armingLive && password.length === 0)}
            className="rounded bg-sky-700 px-3 py-1.5 text-sm font-semibold text-white hover:bg-sky-600 disabled:opacity-50"
          >
            {save.isPending ? 'Saving…' : 'Save configuration'}
          </button>
          <button
            type="button"
            onClick={() => {
              setDraft(toDraft(screen.saved.config))
              setPassword('')
              setLocalError(null)
              save.reset()
            }}
            className="text-xs text-slate-400 hover:text-slate-200"
          >
            Discard changes
          </button>
          <span className="text-xs text-slate-500">
            Takes effect at the worker's next start, not immediately.
          </span>

          {failure ? (
            <p role="alert" className="w-full text-xs text-rose-300">
              {failure}
            </p>
          ) : null}
          {save.isSuccess && failure === null ? (
            <p role="status" className="w-full text-xs text-emerald-300">
              Saved as revision {screen.saved.revision}.
            </p>
          ) : null}
        </div>
      </form>
    </section>
  )
}
