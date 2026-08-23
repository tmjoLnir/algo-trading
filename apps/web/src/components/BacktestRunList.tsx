/**
 * The run list — every backtest, newest first.
 *
 * Four states per row and each renders as itself, which is the whole job of this
 * component:
 *
 * - **queued** — accepted, nothing has picked it up. `started_at` is genuinely
 *   null, so the row says "waiting" rather than showing an elapsed time counted
 *   from a timestamp nobody wrote.
 * - **running** — a progress bar from the server's own `fraction`, with the bar
 *   counts beside it. A percentage on its own cannot tell a slow run from one
 *   whose range turned out to hold forty bars.
 * - **done** — the headline metrics, and any reason to distrust them. The
 *   warnings come from the server so that a number a reader has already seen
 *   arrives already caveated.
 * - **failed** — the reason, in words, on the row. A run stuck at "running"
 *   forever is the worst outcome here and a run that says "failed" with no reason
 *   is the second worst.
 *
 * Metrics are float statistics and go through `stats.ts`; the starting cash on
 * the spec is a decimal string and goes through `money.ts`. The two never mix —
 * see the header of `lib/stats.ts` for why that boundary is a compiler-enforced
 * one.
 *
 * Every row can also be written to a `.json` file, independently of every other
 * — the run as the API served it, plus its equity curve and its trades. That is
 * per row rather than one export of the whole list because what a reader wants
 * to keep, diff or hand to a notebook is a *result*, and a list of forty of them
 * with the curves attached is not a file anybody opens. `lib/backtestExport.ts`
 * owns what goes in it.
 */

import { ApiError } from '@/api/client'
import { formatDateTime, formatMoney } from '@/lib/money'
import { formatCount, formatStat, formatStatPercent, statTone } from '@/lib/stats'
import { isInFlight, useDownloadBacktest } from '@/hooks/useBacktests'
import { hasResultBody } from '@/lib/backtestExport'
import type { BacktestOut } from '@/api/types'

interface Props {
  runs: BacktestOut[]
  selected: string[]
  onSelect: (runId: string) => void
  onToggleCompare: (runId: string) => void
}

const STATUS_TONE: Record<string, string> = {
  done: 'text-emerald-400',
  running: 'text-sky-400',
  queued: 'text-slate-400',
  failed: 'text-amber-400',
}

/** The status word, and for a running job the bar beside it. */
function Status({ run }: { run: BacktestOut }) {
  const tone = STATUS_TONE[run.status] ?? 'text-slate-300'
  return (
    <>
      <span className={`font-medium ${tone}`}>{run.status}</span>
      {run.status === 'running' ? (
        run.progress ? (
          <div className="mt-1 w-32">
            <div className="h-1 overflow-hidden rounded bg-slate-800">
              <div
                className="h-full bg-sky-500"
                // Inline because the width is data. Rounded for the pixels only;
                // the counts below are the exact figures.
                style={{ width: `${Math.round(run.progress.fraction * 100)}%` }}
              />
            </div>
            <div className="mt-0.5 text-[11px] tabular-nums text-slate-500">
              {formatCount(run.progress.bars_done)} / {formatCount(run.progress.bars_total)} bars
            </div>
          </div>
        ) : (
          // Running, and nothing published yet — the job has claimed the row and
          // has not reached its first report, or the progress record expired.
          // Said as itself rather than shown as 0%, which would read as stalled.
          <div className="mt-0.5 text-[11px] text-slate-600">no progress reported yet</div>
        )
      ) : null}
      {run.status === 'queued' ? (
        <div className="mt-0.5 text-[11px] text-slate-600">waiting for a worker</div>
      ) : null}
    </>
  )
}

/** The two figures worth putting on a row of a list. The rest is in the detail. */
function Headline({ run }: { run: BacktestOut }) {
  if (run.status === 'failed') {
    return (
      <span className="text-xs text-amber-300" title={run.error ?? undefined}>
        {run.error ?? 'failed with no reason recorded'}
      </span>
    )
  }
  if (!run.metrics) {
    return <span className="text-xs text-slate-600">—</span>
  }
  const total = run.metrics.total_return ?? null
  const sharpe = run.metrics.sharpe ?? null
  return (
    <span className="text-xs tabular-nums">
      <span className={statTone(total)}>{formatStatPercent(total, { signed: true })}</span>
      <span className="text-slate-600"> · Sharpe </span>
      <span className="text-slate-300">{formatStat(sharpe)}</span>
      <span className="text-slate-600">
        {' '}
        · {formatCount(run.metrics.num_trades ?? null)} trades
      </span>
    </span>
  )
}

/**
 * Write this one run to a file.
 *
 * Its own component so the hook is per row: a slow export of a five-year minute
 * run must not grey out the button on the thirty rows beside it, and a failure
 * belongs on the row that failed rather than on the panel.
 *
 * Offered on every row, including the ones with nothing computed yet. A queued
 * run's file is its spec and its status, which is a smaller thing than a result
 * and still the answer to "what exactly did I ask for" — and the file says which
 * it is, so nothing has to be inferred from its size. The title says so before
 * the click rather than leaving it to be discovered after it.
 *
 * Not disabled for a read-only session, unlike the queue button on this screen:
 * reading a result and writing it to disk performs no act (ADR 0009).
 *
 * **Both outcomes are stated on the row**, and the successful one is not
 * redundant: a browser saving straight to a downloads folder shows nothing at
 * all, and a button that answers a click with silence reads as broken. It says
 * `saved` rather than the name, which does not fit a column this narrow — the
 * name is the title, and it is the one thing needed to find the file.
 */
function ExportButton({ run }: { run: BacktestOut }) {
  const download = useDownloadBacktest()
  return (
    <>
      <button
        type="button"
        onClick={() => download.mutate(run)}
        disabled={download.isPending}
        aria-label={`download run ${run.id} as JSON`}
        title={
          hasResultBody(run)
            ? 'Download this run as .json — the spec, the metrics, the equity curve and every trade.'
            : 'Download this run as .json. It has no result yet, so the file carries the spec and the status.'
        }
        className="rounded border border-slate-700 px-2 py-1 text-[11px] font-medium text-slate-300 hover:border-slate-500 hover:text-slate-100 disabled:opacity-40"
      >
        {download.isPending ? 'saving…' : 'JSON'}
      </button>
      {download.error ? (
        // On the row, because it is this run that could not be written and the
        // others may have exported fine. The reason is in the title: it is the
        // API's own words and too long for a column this narrow.
        <div
          className="mt-1 text-[11px] text-amber-400"
          title={
            download.error instanceof ApiError ? download.error.detail : String(download.error)
          }
        >
          could not export
        </div>
      ) : download.data ? (
        <div className="mt-1 text-[11px] text-emerald-500/80" title={download.data}>
          saved
        </div>
      ) : null}
    </>
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

export default function BacktestRunList({ runs, selected, onSelect, onToggleCompare }: Props) {
  if (runs.length === 0) {
    return (
      <p className="px-4 py-6 text-center text-sm text-slate-500">
        No backtest has been queued yet.
        <span className="mt-1 block text-xs">
          Runs are recorded permanently, so this is also the record of what a strategy was ever
          evaluated on.
        </span>
      </p>
    )
  }

  return (
    <div className="overflow-x-auto">
      <table className="w-full text-sm">
        <thead>
          <tr className="bg-slate-900/60">
            <Header>Compare</Header>
            <Header>Strategy</Header>
            <Header>Window</Header>
            <Header>Status</Header>
            <Header>Result</Header>
            <Header align="right">Queued</Header>
            <Header align="right">Export</Header>
          </tr>
        </thead>
        <tbody>
          {runs.map((run) => (
            <tr
              key={run.id}
              className="cursor-pointer border-t border-slate-800/70 align-top hover:bg-slate-800/30"
              onClick={() => onSelect(run.id)}
            >
              <td className="px-3 py-2" onClick={(event) => event.stopPropagation()}>
                <input
                  type="checkbox"
                  aria-label={`compare ${run.id}`}
                  checked={selected.includes(run.id)}
                  // Only a finished run has metrics to compare; the endpoint
                  // refuses the rest, so the checkbox does not offer it.
                  disabled={run.status !== 'done'}
                  onChange={() => onToggleCompare(run.id)}
                  className="h-3.5 w-3.5 accent-sky-500 disabled:opacity-30"
                />
              </td>
              <td className="px-3 py-2 text-left font-medium text-slate-100">
                {run.strategy_id}
                <div className="text-xs text-slate-500">
                  {(run.spec.symbols ?? []).join(', ')} · {run.spec.timeframe}
                </div>
                <div className="text-[11px] text-slate-600">
                  {formatMoney(run.spec.starting_cash, { places: 0 })} · {run.spec.qty} sh/entry
                  {run.spec.cost_model === 'zero' ? (
                    // Loud, because it invalidates everything on the row.
                    <span className="ml-1 text-amber-400">· zero cost</span>
                  ) : null}
                </div>
              </td>
              <td className="whitespace-nowrap px-3 py-2 text-left text-xs text-slate-400">
                {run.spec.start.slice(0, 10)}
                <div className="text-slate-600">→ {run.spec.end.slice(0, 10)}</div>
              </td>
              <td className="px-3 py-2 text-left text-xs">
                <Status run={run} />
              </td>
              <td className="px-3 py-2 text-left">
                <Headline run={run} />
                {(run.warnings ?? []).length > 0 ? (
                  <div className="mt-1 text-[11px] text-amber-300/80">
                    ⚠ {(run.warnings ?? []).length} caveat
                    {(run.warnings ?? []).length === 1 ? '' : 's'} — open the run
                  </div>
                ) : null}
              </td>
              <td className="whitespace-nowrap px-3 py-2 text-right text-xs text-slate-400 tabular-nums">
                {formatDateTime(run.queued_at)}
                {/* Three timestamps, and the list shows the one that is never
                    null. A queued run has no start and no finish, which is what
                    the status column already says. */}
                {run.finished_at ? (
                  <div className="text-slate-600">finished {formatDateTime(run.finished_at)}</div>
                ) : isInFlight(run) ? (
                  <div className="text-slate-600">not finished</div>
                ) : null}
              </td>
              {/* Stops the click here, like the compare cell: exporting a run is
                  not asking to open it, and a panel that sprang open on every
                  download would scroll the list out from under the next one. */}
              <td
                className="whitespace-nowrap px-3 py-2 text-right"
                onClick={(event) => event.stopPropagation()}
              >
                <ExportButton run={run} />
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}
