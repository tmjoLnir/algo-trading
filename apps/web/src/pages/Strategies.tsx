/**
 * Strategies — what the code knows about, and what a worker has actually run.
 *
 * **The gap between those two is the reason this screen exists.** A strategy
 * class is registered at import time; a `strategies` row is written by the
 * runner at its first session open. `WORKER_STRATEGY` is empty by default, so
 * the ordinary state of a fresh install is a platform with strategies in it and
 * nothing running — and until now no screen could tell a reader whether the
 * thing they configured had ever been picked up. "I wrote a strategy and
 * nothing is happening" had no answer anywhere in this UI.
 *
 * Two labels on this page are deliberately not the column names behind them,
 * because the columns mean less than they say:
 *
 * - **`state` is not "is it running now".** `StrategyRepository.ensure` writes
 *   `draft` when it creates a row and never touches it again, so a strategy a
 *   worker has been running for a month still reads `draft` — that is the
 *   ratchet's first rung and nothing has promoted it off, because the endpoints
 *   that would are stubs. It is shown as the *configured* state, with the
 *   liveness question answered by the timestamp beside it instead. It wrote
 *   `active` until #PR, which was not a member of `StrategyState` at all.
 * - **`updated_at` is not "last edited".** The same asymmetry: a later boot
 *   bumps only the timestamp. The API serves it as `last_started_at` and this
 *   screen renders it as "a worker last started this", which is what it records.
 *
 * No actions. Creating, editing, promoting and pausing are the promotion
 * ratchet, and half its preconditions cannot be checked yet — there is no
 * stored backtest to require, and the audit trail's lifecycle verbs are unwired
 * (ADR 0010). A promote button that skipped the checks it could not perform
 * would be the ratchet with its pawl removed.
 *
 * **The risk limits live here too**, at the top, rather than on a Risk tab of
 * their own. `docs/ROADMAP.md` describes `/risk/status` as "what a human checks
 * before promoting to live", and this is the screen that promotion decision is
 * made on — so "has this ever run" and "how much of the exposure ceiling is
 * already spent" belong in one view rather than one nav click apart. The nav
 * stays at seven tabs.
 */

import { useState } from 'react'
import { ApiError } from '@/api/client'
import RiskLimitsPanel from '@/components/RiskLimitsPanel'
import { STRATEGY_STATES, useStrategies } from '@/hooks/useStrategies'
import { UNKNOWN, formatDateTime } from '@/lib/money'
import type { AvailableStrategyView, StoredStrategyView, StrategyState } from '@/api/types'

/**
 * Tint per rung. The word is always present; colour is an accent.
 *
 * A `Record` over the generated union, so a rung added to the server's enum
 * fails `tsc` here until somebody decides what colour it is. It was
 * `Record<string, string>` and had drifted: it tinted `active` and `backtest`,
 * neither of them members, and had nothing for `live` or `halted`.
 */
const STATE_TONE: Record<StrategyState, string> = {
  draft: 'text-slate-400',
  backtesting: 'text-slate-300',
  paper: 'text-sky-400',
  live: 'text-emerald-400',
  paused: 'text-amber-400',
  halted: 'text-rose-400',
}

/**
 * The tint for a stored state, which is not guaranteed to be a rung.
 *
 * `StoredStrategyView.state` is deliberately a bare string on the wire: a
 * database that has not run the `e2b6d1a70f93` migration still holds `active`,
 * and a row written by a newer server may hold a rung this build has never
 * heard of. Both render in neutral rather than crashing the row — the word
 * itself is displayed either way, which is the part a reader needs.
 */
function toneFor(state: string): string {
  return STATE_TONE[state as StrategyState] ?? 'text-slate-300'
}

function Panel({
  title,
  subtitle,
  control,
  children,
}: {
  title: string
  subtitle?: string
  control?: React.ReactNode
  children: React.ReactNode
}) {
  return (
    <section className="rounded border border-slate-800 bg-slate-900/20">
      <div className="flex flex-wrap items-baseline justify-between gap-2 px-4 py-3">
        <div>
          <h2 className="text-sm font-semibold text-slate-300">{title}</h2>
          {subtitle ? <p className="mt-0.5 text-xs text-slate-500">{subtitle}</p> : null}
        </div>
        {control}
      </div>
      {children}
    </section>
  )
}

/** A `params` or `risk_config` object, compactly. */
function KeyValues({ values }: { values: Record<string, unknown> }) {
  const entries = Object.entries(values)
  if (entries.length === 0) return <span className="text-slate-600">{UNKNOWN}</span>
  return (
    <>
      {entries.map(([key, value]) => (
        <span key={key} className="mr-3 whitespace-nowrap">
          {key}=<span className="text-slate-400">{String(value)}</span>
        </span>
      ))}
    </>
  )
}

function StoredRow({ strategy }: { strategy: StoredStrategyView }) {
  // Optional in the generated schema because the server model defaults it.
  // Resolved once, so the branch below compares a list rather than a maybe-list.
  const universe = strategy.universe ?? []
  return (
    <tr className="border-t border-slate-800/70 align-top hover:bg-slate-800/30">
      <td className="px-3 py-2 text-left font-medium text-slate-100">
        {strategy.name}
        <div className="text-xs text-slate-500">
          {strategy.kind === 'coded' ? (strategy.class_name ?? 'coded') : 'declarative ruleset'}
          {' · '}
          {strategy.timeframe}
        </div>
        {strategy.description ? (
          <div className="mt-1 max-w-md text-xs text-slate-500">{strategy.description}</div>
        ) : null}
      </td>
      <td className="px-3 py-2 text-left">
        <span className={`font-medium ${toneFor(strategy.state)}`}>{strategy.state}</span>
        {/* Said out loud, because the state is written once and never
            revisited: a strategy a worker has been running for a month still
            reads whatever its first boot set, which today is always `draft`. */}
        <div className="text-xs text-slate-600">as configured</div>
      </td>
      <td className="px-3 py-2 text-left text-xs text-slate-400">
        {universe.length === 0 ? (
          <span className="text-slate-600">{UNKNOWN}</span>
        ) : (
          universe.join(', ')
        )}
      </td>
      <td className="px-3 py-2 text-left text-xs text-slate-500">
        <KeyValues values={strategy.params ?? {}} />
        {strategy.ruleset ? (
          // For a declarative strategy the ruleset *is* the strategy, so a
          // screen omitting it would be useless for exactly those.
          <pre className="mt-1 max-w-md overflow-x-auto rounded bg-slate-950/60 p-2 text-[11px] text-slate-400">
            {JSON.stringify(strategy.ruleset, null, 2)}
          </pre>
        ) : null}
      </td>
      <td className="px-3 py-2 text-left text-xs text-slate-500">
        <KeyValues values={strategy.risk_config ?? {}} />
      </td>
      <td className="whitespace-nowrap px-3 py-2 text-right text-xs text-slate-400 tabular-nums">
        {formatDateTime(strategy.last_started_at)}
        <div className="text-slate-600">created {formatDateTime(strategy.created_at)}</div>
      </td>
    </tr>
  )
}

function Header({ children, align = 'left' }: { children: string; align?: 'left' | 'right' }) {
  return (
    <th
      className={`px-3 py-2 text-xs font-medium uppercase tracking-wide text-slate-500 ${
        align === 'left' ? 'text-left' : 'text-right'
      }`}
    >
      {children}
    </th>
  )
}

function AvailableRow({ entry }: { entry: AvailableStrategyView }) {
  return (
    <tr className="border-t border-slate-800/70 hover:bg-slate-800/30">
      <td className="px-3 py-2 text-left font-medium text-slate-100">
        {entry.name}
        <div className="text-xs text-slate-500">{entry.class_name}</div>
      </td>
      <td className="px-3 py-2 text-left text-xs text-slate-500">
        {entry.description || <span className="text-slate-600">{UNKNOWN}</span>}
      </td>
      <td className="px-3 py-2 text-left text-xs">
        {entry.has_run ? (
          <span className="text-emerald-400">a worker has run this</span>
        ) : (
          // The row this panel exists for.
          <span className="text-amber-400">never run</span>
        )}
      </td>
    </tr>
  )
}

export default function Strategies() {
  const [state, setState] = useState('')
  const query = useStrategies(state)

  const strategies = query.data?.strategies ?? []
  const available = query.data?.available ?? []
  const neverRun = query.data?.never_run ?? []

  // The risk panel is rendered on every branch below, including the two that
  // give up on the strategy list. The limits are a separate request against a
  // separate store, and a strategies query that failed says nothing about
  // whether the book can be read — blanking the whole page for it would hide a
  // working answer behind an unrelated failure.
  if (query.isLoading) {
    return (
      <div className="space-y-4">
        <RiskLimitsPanel />
        <p className="p-8 text-sm text-slate-400">Loading…</p>
      </div>
    )
  }

  if (query.error) {
    return (
      <div className="space-y-4">
        <RiskLimitsPanel />
        <p className="p-8 text-sm text-amber-400">
          Could not load the strategies.
          <span className="mt-1 block text-xs text-amber-200/70">
            {query.error instanceof ApiError ? query.error.detail : String(query.error)}
          </span>
        </p>
      </div>
    )
  }

  return (
    <div className="space-y-4">
      <RiskLimitsPanel />

      {neverRun.length > 0 ? (
        // The answer to "I wrote a strategy and nothing is happening", which
        // no other screen in this UI could give.
        <p className="rounded border border-amber-700/60 bg-amber-950/30 px-3 py-2 text-sm text-amber-200">
          ⚠ {neverRun.join(', ')} {neverRun.length === 1 ? 'exists' : 'exist'} in the code and{' '}
          {neverRun.length === 1 ? 'has' : 'have'} never been run by a worker.
          <span className="mt-1 block text-xs text-amber-200/70">
            A strategy only gets a row here once a worker loads it.{' '}
            <code className="text-amber-100">WORKER_STRATEGY</code> is empty by default, so on a
            fresh install this is expected rather than a fault.
          </span>
        </p>
      ) : null}

      <Panel
        title="Strategies a worker has run"
        subtitle="One row per strategy the runner has registered at a session open."
        control={
          <div>
            <label className="sr-only" htmlFor="state-filter">
              Filter by state
            </label>
            <select
              id="state-filter"
              value={state}
              onChange={(event) => setState(event.target.value)}
              className="rounded border border-slate-700 bg-slate-900 px-2 py-1 text-xs text-slate-300"
            >
              {STRATEGY_STATES.map((option) => (
                <option key={option.value} value={option.value}>
                  {option.label}
                </option>
              ))}
            </select>
          </div>
        }
      >
        {strategies.length === 0 ? (
          <p className="px-4 py-6 text-center text-sm text-slate-500">
            {state
              ? `No strategy is in the "${state}" state.`
              : 'No worker has registered a strategy yet.'}
            <span className="mt-1 block text-xs">
              This is the table a worker writes to at every session open — an empty one means
              nothing has run, not that nothing exists. What exists is below.
            </span>
          </p>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="bg-slate-900/60">
                  <Header>Strategy</Header>
                  <Header>State</Header>
                  <Header>Universe</Header>
                  <Header>Parameters</Header>
                  <Header>Risk config</Header>
                  <Header align="right">A worker last started this</Header>
                </tr>
              </thead>
              <tbody>
                {strategies.map((strategy) => (
                  <StoredRow key={strategy.id} strategy={strategy} />
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Panel>

      <Panel
        title="Strategy classes in the code"
        subtitle="The registry — what could run. A class is registered when its module is imported, whether or not anything ever loads it."
      >
        {available.length === 0 ? (
          <p className="px-4 py-6 text-center text-sm text-slate-500">
            No strategy classes are registered in this process.
          </p>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="bg-slate-900/60">
                  <Header>Class</Header>
                  <Header>Description</Header>
                  <Header>Has it run?</Header>
                </tr>
              </thead>
              <tbody>
                {available.map((entry) => (
                  <AvailableRow key={entry.name} entry={entry} />
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Panel>

      <p className="text-xs text-slate-500">
        Read-only. Creating, editing, promoting and pausing a strategy are the promotion ratchet —
        draft → backtest → paper → live — and half its preconditions cannot be checked yet: there is
        no stored backtest to require, and the audit trail cannot yet record who promoted what. Per
        strategy P&amp;L is on the Analytics tab, grouped by strategy.
      </p>
    </div>
  )
}
