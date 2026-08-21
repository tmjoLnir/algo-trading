/**
 * What the risk chain refused, and what it cannot tell you it refused.
 *
 * On the Strategies tab beside the limits panel, because the two answer halves
 * of one question. "Why is this strategy doing nothing" is either *it has had
 * no ideas* or *every idea was refused*, and from the orders table alone those
 * are indistinguishable — a refused signal never becomes an order. This is the
 * only screen where the difference is visible.
 *
 * **An empty list is the dangerous state, not the reassuring one**, and the
 * panel is built around that. Nothing refused and nothing recorded look
 * identical here, so the blind spots the server sends travel with the data and
 * are rendered whether or not there is anything above them. A stop exit the
 * risk chain denied is written to the worker's log and stored nowhere — that is
 * a position which should have closed and did not, and it will never appear in
 * this table.
 *
 * `by_rule` counts the rows fetched, not all history, and says so. "Which rule
 * is refusing everything" is the question, and over the most recent refusals
 * that is a fair answer as long as the screen does not imply it is a total.
 */

import { ApiError } from '@/api/client'
import { useRejections } from '@/hooks/useRejections'
import { UNKNOWN, formatDateTime } from '@/lib/money'
import type { RejectionView } from '@/api/types'

/** The rule name, as the engine writes it — the string a reader can grep for. */
function Rule({ name }: { name: string }) {
  return (
    <span className="rounded bg-slate-800 px-1.5 py-0.5 font-mono text-[11px] text-amber-300">
      {name}
    </span>
  )
}

function Row({ rejection }: { rejection: RejectionView }) {
  const indicators = Object.entries(rejection.indicators ?? {})
  return (
    <tr className="border-t border-slate-800/70 align-top hover:bg-slate-800/30">
      <td className="whitespace-nowrap px-3 py-2 text-left text-xs text-slate-400 tabular-nums">
        {formatDateTime(rejection.at)}
      </td>
      <td className="px-3 py-2 text-left font-medium text-slate-100">
        {rejection.symbol}
        <div className="text-xs text-slate-500">{rejection.action.replace(/_/g, ' ')}</div>
      </td>
      <td className="px-3 py-2 text-left">
        <Rule name={rejection.rule} />
      </td>
      <td className="px-3 py-2 text-left text-xs text-slate-400">
        {rejection.reason || <span className="text-slate-600">{UNKNOWN}</span>}
        {indicators.length > 0 ? (
          // What the strategy was looking at when it decided — the thing that
          // makes a refusal diagnosable months later, when the bar series has
          // been restated and the indicator cannot be recomputed.
          <div className="mt-1 text-[11px] text-slate-600">
            {indicators.map(([key, value]) => (
              <span key={key} className="mr-3 whitespace-nowrap">
                {key}=<span className="text-slate-500">{value}</span>
              </span>
            ))}
          </div>
        ) : null}
      </td>
      <td className="px-3 py-2 text-left text-xs text-slate-500">{rejection.strategy_id}</td>
    </tr>
  )
}

export default function RejectionsPanel() {
  const query = useRejections()
  const rejections = query.data?.rejections ?? []
  const byRule = Object.entries(query.data?.by_rule ?? {})
  const blindSpots = query.data?.blind_spots ?? []

  return (
    <section className="rounded border border-slate-800 bg-slate-900/20">
      <div className="flex flex-wrap items-baseline justify-between gap-2 px-4 py-3">
        <div>
          <h2 className="text-sm font-semibold text-slate-300">Refused decisions</h2>
          <p className="mt-0.5 text-xs text-slate-500">
            Signals the risk chain denied. A strategy whose every idea was refused looks, from the
            orders table alone, exactly like a strategy that had no ideas.
          </p>
        </div>
        {byRule.length > 0 ? (
          <div className="flex flex-wrap items-center gap-2 text-xs text-slate-500">
            {byRule.map(([rule, count]) => (
              <span key={rule}>
                <Rule name={rule} /> <span className="tabular-nums text-slate-400">{count}</span>
              </span>
            ))}
            <span className="text-slate-600">of the {rejections.length} shown</span>
          </div>
        ) : null}
      </div>

      {query.isError ? (
        <p
          role="alert"
          className="mx-4 mb-3 rounded border border-amber-700/60 bg-amber-950/30 px-3 py-2 text-xs text-amber-200"
        >
          ⚠ Could not read the refusals, so this screen cannot say whether anything is being blocked
          — which is not the same as nothing being blocked.
          <span className="mt-1 block text-amber-200/70">
            {query.error instanceof ApiError ? query.error.detail : String(query.error)}
          </span>
        </p>
      ) : null}

      {query.isLoading ? (
        <p className="px-4 py-6 text-center text-sm text-slate-500">Loading…</p>
      ) : rejections.length === 0 && !query.isError ? (
        <p className="px-4 py-6 text-center text-sm text-slate-500">
          No refusal is recorded.
          <span className="mt-1 block text-xs">
            Read that with the note below: it means nothing <em>of the kind this table holds</em>{' '}
            has been refused.
          </span>
        </p>
      ) : rejections.length > 0 ? (
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="bg-slate-900/60">
                {['When', 'Symbol', 'Refused by', 'Why', 'Strategy'].map((heading) => (
                  <th
                    key={heading}
                    className="px-3 py-2 text-left text-xs font-medium uppercase tracking-wide text-slate-500"
                  >
                    {heading}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {rejections.map((rejection) => (
                <Row key={rejection.signal_id} rejection={rejection} />
              ))}
            </tbody>
          </table>
        </div>
      ) : null}

      {blindSpots.length > 0 ? (
        // Rendered whether or not there are rows above, and that is the point:
        // an empty table reads as "nothing is being refused", and these are the
        // refusals no query can find.
        <div className="border-t border-slate-800/70 px-4 py-3">
          <p className="text-xs font-medium text-slate-400">What this table cannot show</p>
          <ul className="mt-1 space-y-1">
            {blindSpots.map((spot) => (
              <li key={spot} className="text-xs text-slate-500">
                • {spot}
              </li>
            ))}
          </ul>
        </div>
      ) : null}
    </section>
  )
}
