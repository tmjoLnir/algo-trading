/**
 * Metrics for several runs side by side.
 *
 * **The warning is not decoration and it is not dismissible.** Comparing variants
 * and picking the best is how overfitting happens — the winner of a sweep is
 * usually the luckiest parameter set, not the best one (docs/BACKTESTING.md
 * 'Overfitting'). The server sends the sentence and this renders it above the
 * table, because a table read to pick a winner is exactly where somebody is
 * about to make that mistake.
 *
 * The server also caps the comparison at a handful of runs, and that cap is the
 * same argument expressed as a limit rather than as advice: this panel is
 * deliberately useless for sweeping.
 *
 * Best-per-row is deliberately **not** highlighted. Colouring the winner of each
 * metric is the interface telling a reader which parameter set to choose, which
 * is the exact behaviour the warning above it is asking them not to perform.
 */

import { useBacktestComparison } from '@/hooks/useBacktests'
import { formatMetric } from '@/lib/stats'

interface Props {
  runIds: string[]
  onClear: () => void
}

export default function BacktestComparison({ runIds, onClear }: Props) {
  const { data, isLoading, error } = useBacktestComparison(runIds)

  if (runIds.length < 2) return null

  return (
    <section className="rounded border border-slate-700 bg-slate-900/40">
      <div className="flex flex-wrap items-baseline justify-between gap-2 border-b border-slate-800 px-4 py-3">
        <h2 className="text-sm font-semibold text-slate-200">Comparing {runIds.length} runs</h2>
        <button
          type="button"
          onClick={onClear}
          className="rounded border border-slate-700 px-2 py-1 text-xs text-slate-400 hover:text-slate-200"
        >
          Clear selection
        </button>
      </div>

      {isLoading ? (
        <p className="px-4 py-6 text-center text-xs text-slate-500">Loading…</p>
      ) : error ? (
        <p className="px-4 py-6 text-center text-xs text-amber-400">
          Could not compare these runs.
        </p>
      ) : !data ? null : (
        <>
          <p className="border-b border-amber-800/40 bg-amber-950/20 px-4 py-2 text-xs text-amber-200">
            ⚠ {data.overfitting_warning}
          </p>
          <div className="overflow-x-auto">
            <table className="w-full text-xs">
              <thead>
                <tr className="bg-slate-900/60 text-slate-500">
                  <th className="px-3 py-2 text-left font-medium">Metric</th>
                  {data.runs.map((run) => (
                    <th key={run.id} className="px-3 py-2 text-right font-medium">
                      <span className="text-slate-300">{run.strategy_id}</span>
                      <div className="font-normal text-slate-600">
                        {(run.spec.symbols ?? []).join(',')} · {run.spec.timeframe}
                      </div>
                      <div className="font-normal text-slate-600">
                        {run.spec.start.slice(0, 10)} → {run.spec.end.slice(0, 10)}
                      </div>
                      {run.spec.cost_model === 'zero' ? (
                        <div className="font-normal text-amber-400">zero cost</div>
                      ) : null}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {Object.entries(data.metrics).map(([name, byRun]) => (
                  <tr key={name} className="border-t border-slate-800/70">
                    <td className="px-3 py-1.5 text-left text-slate-400">{name}</td>
                    {data.runs.map((run) => (
                      <td
                        key={run.id}
                        className="px-3 py-1.5 text-right tabular-nums text-slate-200"
                      >
                        {formatMetric(name, byRun[run.id])}
                      </td>
                    ))}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <p className="px-4 py-2 text-[11px] text-slate-600">
            No column is marked as the winner, on purpose. Highlighting the best value per row would
            be this screen making the choice the warning above asks you not to make on these numbers
            alone.
          </p>
        </>
      )}
    </section>
  )
}
