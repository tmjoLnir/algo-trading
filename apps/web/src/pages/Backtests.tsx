/**
 * Backtests — the last of the seven tabs, and the only one that starts work.
 *
 * Every other screen in this app reads something that already happened. This one
 * queues a job, watches it run, and then shows what it produced. Three
 * consequences shape the page:
 *
 * - **It is the only screen with a form**, and the form's job is mostly to stay
 *   out of the way of the server's validation. The one refusal worth designing
 *   around is missing history: the API names the exact `backfill_bars.py` command
 *   that fixes it, and `BacktestForm` shows that verbatim rather than
 *   paraphrasing an actionable message into a dead end.
 * - **It polls only while something is in flight.** A queued run changes within
 *   seconds and then never again, so `useBacktests` derives the interval from the
 *   data and stops the timer entirely once every run is terminal. A tab left open
 *   on finished runs makes no requests.
 * - **It can only offer strategies a worker has run.** `backtest_runs.strategy_id`
 *   is a foreign key onto `strategies`, written by the runner at its first
 *   session open — so the picker is built from that table, and the registered
 *   classes that have never run are named separately with what to do about them.
 *   This is the same gap the Strategies tab exists to show, met from the other
 *   side.
 *
 * What this screen made possible elsewhere, now that a backtest can be stored:
 * `/analytics/live-vs-backtest` got its second operand and is now an endpoint —
 * keyed on a run id, because which backtest a live record is judged against is
 * the whole substance of that comparison. It has no screen yet, and the run
 * picker it wants is this page's list. The promotion ratchet can also begin to
 * ask for "a completed backtest on record". Both are their own roadmap items.
 */

import { useState } from 'react'
import { ApiError } from '@/api/client'
import BacktestComparison from '@/components/BacktestComparison'
import BacktestDetail from '@/components/BacktestDetail'
import BacktestForm from '@/components/BacktestForm'
import BacktestRunList from '@/components/BacktestRunList'
import { useBacktests } from '@/hooks/useBacktests'
import { useStrategies } from '@/hooks/useStrategies'
import { useSession } from '@/api/session'

export default function Backtests() {
  const [strategyFilter, setStrategyFilter] = useState('')
  const [openRunId, setOpenRunId] = useState<string | null>(null)
  const [comparing, setComparing] = useState<string[]>([])

  const { mayAct } = useSession()
  // Unfiltered on purpose: the form needs every strategy that *can* be
  // backtested regardless of which one the list below is filtered to.
  const strategies = useStrategies('')
  const query = useBacktests(strategyFilter)

  const runs = query.data?.runs ?? []
  const openRun = runs.find((run) => run.id === openRunId) ?? null

  const toggleCompare = (runId: string) =>
    setComparing((current) =>
      current.includes(runId) ? current.filter((id) => id !== runId) : [...current, runId],
    )

  return (
    <div className="space-y-4">
      <BacktestForm
        runnable={strategies.data?.strategies ?? []}
        available={strategies.data?.available ?? []}
        neverRun={strategies.data?.never_run ?? []}
        mayAct={mayAct}
      />

      {comparing.length >= 2 ? (
        <BacktestComparison runIds={comparing} onClear={() => setComparing([])} />
      ) : null}

      {openRun ? <BacktestDetail run={openRun} onClose={() => setOpenRunId(null)} /> : null}

      <section className="rounded border border-slate-800 bg-slate-900/20">
        <div className="flex flex-wrap items-baseline justify-between gap-2 px-4 py-3">
          <div>
            <h2 className="text-sm font-semibold text-slate-300">Runs</h2>
            <p className="mt-0.5 text-xs text-slate-500">
              Newest first. Select a row to open it; tick two or more finished runs to compare them;
              export any one of them to a .json file with the button on its row.
              {runs.some((run) => run.status === 'queued' || run.status === 'running')
                ? ' Updating while a run is in flight.'
                : ''}
            </p>
          </div>
          <div>
            <label className="sr-only" htmlFor="backtest-strategy-filter">
              Filter by strategy
            </label>
            <select
              id="backtest-strategy-filter"
              value={strategyFilter}
              onChange={(event) => setStrategyFilter(event.target.value)}
              className="rounded border border-slate-700 bg-slate-900 px-2 py-1 text-xs text-slate-300"
            >
              <option value="">Every strategy</option>
              {(strategies.data?.strategies ?? []).map((strategy) => (
                <option key={strategy.id} value={strategy.id}>
                  {strategy.name}
                </option>
              ))}
            </select>
          </div>
        </div>

        {query.isLoading ? (
          <p className="px-4 py-6 text-center text-sm text-slate-400">Loading…</p>
        ) : query.error ? (
          <p className="px-4 py-6 text-center text-sm text-amber-400">
            Could not load the backtest runs.
            <span className="mt-1 block text-xs text-amber-200/70">
              {query.error instanceof ApiError ? query.error.detail : String(query.error)}
            </span>
          </p>
        ) : (
          <BacktestRunList
            runs={runs}
            selected={comparing}
            onSelect={setOpenRunId}
            onToggleCompare={toggleCompare}
          />
        )}

        {query.data?.limit_reached ? (
          // Stated rather than inferred, like the orders screen: a list that
          // stops at exactly the limit looks identical to one that ended.
          <p className="px-4 py-2 text-xs text-slate-600">
            Showing the newest runs only — there are older ones this page did not fetch.
          </p>
        ) : null}
      </section>

      <p className="text-xs text-slate-500">
        A run is stored permanently, so this is also the record of what a strategy was ever
        evaluated on. Queued runs are executed by a separate worker process, one at a time; a run
        left behind by a worker that stopped is marked interrupted the next time one starts, rather
        than sitting at &ldquo;running&rdquo; forever.
      </p>
    </div>
  )
}
