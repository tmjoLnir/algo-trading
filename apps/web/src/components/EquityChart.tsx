/**
 * The equity curve — layout item 4.
 *
 * Drawn from `/dashboard/equity-curve`, which is thinned server-side to one
 * point per bucket, keeping the **last** observation in each. Equity is a level
 * rather than a flow, so an average inside a bucket would plot a number the
 * account never actually held.
 *
 * This is the one component that converts money to a JavaScript number, and it
 * does so through `toChartNumber`, which is named to make every other use of it
 * read as a mistake: a chart maps values to pixels and a pixel has no cents.
 * Nothing displayed as text goes through it — the axis labels and the tooltip
 * format the original strings.
 */

import {
  Area,
  AreaChart,
  CartesianGrid,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts'
import { useEquityCurve } from '@/hooks/useLiveDashboard'
import { formatDateTime, formatMoney, toChartNumber } from '@/lib/money'
import type { EquityPointView } from '@/api/types'

interface Props {
  days?: number
}

interface Plotted {
  ts: string
  /** Pixels, not money. See the module docstring. */
  equity: number
  /** The exact value, carried alongside so the tooltip never formats a float. */
  exact: string
}

function toPlotted(points: EquityPointView[]): Plotted[] {
  return points.map((point) => ({
    ts: point.ts,
    equity: toChartNumber(point.equity),
    exact: point.equity,
  }))
}

function EquityTooltip({
  active,
  payload,
}: {
  active?: boolean
  payload?: { payload: Plotted }[]
}) {
  const point = payload?.[0]?.payload
  if (!active || !point) return null
  return (
    <div className="rounded border border-slate-700 bg-slate-900 px-3 py-2 text-xs shadow">
      <div className="tabular-nums text-slate-100">{formatMoney(point.exact)}</div>
      <div className="text-slate-500">{formatDateTime(point.ts)}</div>
    </div>
  )
}

export default function EquityChart({ days = 30 }: Props) {
  const { data, isLoading, error } = useEquityCurve(days)
  const points = toPlotted(data?.points ?? [])

  return (
    <section className="rounded border border-slate-800 bg-slate-900/40 p-4">
      <div className="mb-2 flex items-baseline justify-between">
        <h2 className="text-sm font-semibold text-slate-300">Equity</h2>
        <span className="text-xs text-slate-500">
          last {days} days{data ? ` · ${data.resolution} buckets` : ''}
        </span>
      </div>

      {isLoading ? (
        <p className="py-8 text-center text-sm text-slate-500">Loading…</p>
      ) : error ? (
        // The chart is history, not the book. Its failure says so and leaves the
        // rest of the dashboard alone rather than taking the screen down.
        <p className="py-8 text-center text-sm text-amber-400">
          Could not load the equity history.
        </p>
      ) : points.length === 0 ? (
        <p className="py-8 text-center text-sm text-slate-500">
          No equity recorded yet — the worker writes a point on every evaluation.
        </p>
      ) : (
        <div className="h-56">
          <ResponsiveContainer width="100%" height="100%">
            <AreaChart data={points} margin={{ top: 4, right: 8, bottom: 0, left: 8 }}>
              <defs>
                <linearGradient id="equityFill" x1="0" y1="0" x2="0" y2="1">
                  <stop offset="0%" stopColor="#38bdf8" stopOpacity={0.35} />
                  <stop offset="100%" stopColor="#38bdf8" stopOpacity={0} />
                </linearGradient>
              </defs>
              <CartesianGrid stroke="#1e293b" vertical={false} />
              <XAxis
                dataKey="ts"
                tick={{ fill: '#64748b', fontSize: 11 }}
                tickLine={false}
                axisLine={{ stroke: '#1e293b' }}
                tickFormatter={(iso: string) => new Date(iso).toLocaleDateString()}
                minTickGap={48}
              />
              <YAxis
                tick={{ fill: '#64748b', fontSize: 11 }}
                tickLine={false}
                axisLine={false}
                width={72}
                domain={['auto', 'auto']}
                tickFormatter={(value: number) => formatMoney(String(value), { places: 0 })}
              />
              <Tooltip content={<EquityTooltip />} />
              <Area
                type="monotone"
                dataKey="equity"
                stroke="#38bdf8"
                strokeWidth={2}
                fill="url(#equityFill)"
                isAnimationActive={false}
                dot={false}
              />
            </AreaChart>
          </ResponsiveContainer>
        </div>
      )}
    </section>
  )
}
