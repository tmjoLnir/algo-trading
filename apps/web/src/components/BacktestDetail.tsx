/**
 * One run in full: the metric set, its equity curve, and every simulated trade.
 *
 * The trade table is the part that earns this panel. docs/BACKTESTING.md's
 * pre-belief checklist asks for "individual trades inspected — no impossible
 * fills", and nothing else in this platform can answer it: a metric set cannot
 * show you the one fill that made the number.
 *
 * Two boundaries are held here and both are visible in the imports. The metric
 * set arrives as JSON floats and goes through `stats.ts`; every figure on a trade
 * — price, quantity, fee, P&L — is a decimal string and goes through `money.ts`,
 * which accepts only strings. The five money-shaped *metrics* (`expectancy`,
 * `avg_win`, `avg_loss`, `largest_win`, `largest_loss`) are float statistics and
 * stay in `stats.ts`, labelled as statistics: formatting them with the ledger
 * formatter would claim a precision the response does not carry.
 *
 * The Result block above the metrics is the ledger half and the metric grid is
 * the statistical one, which is why they are two blocks with two formatters. A
 * run that ends still holding its winners reports a return its closed trades
 * never made, and every statistic in the grid counts closed round trips only —
 * so the split, and the sentence under it, is what stops a mark being read as a
 * track record.
 *
 * The curve and the trades are separate requests, fetched only when a finished
 * run is opened. They are large — a minute run's curve is hundreds of thousands
 * of points — and the list this screen polls every three seconds has to stay
 * small.
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
import { useBacktestCurve, useBacktestTrades } from '@/hooks/useBacktests'
import {
  UNKNOWN,
  formatDateTime,
  formatMoney,
  formatPercent,
  toChartNumber,
  toneFor,
} from '@/lib/money'
import { formatCount, formatDuration, formatStat, formatStatPercent, statTone } from '@/lib/stats'
import type { BacktestOut, BacktestTotalsView, BacktestTrade } from '@/api/types'

interface Props {
  run: BacktestOut
  onClose: () => void
}

/**
 * The metric set, in the order docs/BACKTESTING.md 'Reading the result'
 * discusses it — the same order `scripts/run_backtest.py` prints.
 *
 * `money` here means "denominated in account currency and still a float". They
 * are rendered by `formatStat`, not `formatMoney`, and the panel labels the group
 * so a reader knows which kind of number they are looking at.
 */
const METRICS: { key: string; label: string; kind: 'pct' | 'num' | 'int' | 'money' }[] = [
  { key: 'total_return', label: 'Total return', kind: 'pct' },
  { key: 'cagr', label: 'CAGR', kind: 'pct' },
  { key: 'sharpe', label: 'Sharpe', kind: 'num' },
  { key: 'sortino', label: 'Sortino', kind: 'num' },
  { key: 'calmar', label: 'Calmar', kind: 'num' },
  { key: 'volatility', label: 'Volatility (ann.)', kind: 'pct' },
  { key: 'max_drawdown', label: 'Max drawdown', kind: 'pct' },
  { key: 'max_drawdown_duration_days', label: '…lasting (days)', kind: 'int' },
  { key: 'num_trades', label: 'Trades', kind: 'int' },
  { key: 'win_rate', label: 'Win rate', kind: 'pct' },
  { key: 'profit_factor', label: 'Profit factor', kind: 'num' },
  { key: 'expectancy', label: 'Expectancy / trade', kind: 'money' },
  { key: 'avg_win', label: 'Average win', kind: 'money' },
  { key: 'avg_loss', label: 'Average loss', kind: 'money' },
  { key: 'largest_win', label: 'Largest win', kind: 'money' },
  { key: 'largest_loss', label: 'Largest loss', kind: 'money' },
  { key: 'exposure_pct', label: 'Time in market', kind: 'pct' },
  { key: 'turnover', label: 'Turnover (× equity)', kind: 'num' },
]

/** Signed, because for these the sign is the fact. */
const SIGNED = new Set(['total_return', 'cagr', 'expectancy'])

function renderMetric(value: number | null | undefined, kind: string, signed: boolean): string {
  if (kind === 'pct') return formatStatPercent(value, { signed })
  if (kind === 'int') return formatCount(value)
  // `money` falls through to the same formatter as `num` on purpose: these are
  // floats, and `formatMoney` takes only strings so the compiler agrees.
  return formatStat(value, { signed })
}

function MetricGrid({ metrics }: { metrics: Record<string, number | null> }) {
  return (
    <dl className="grid grid-cols-2 gap-x-6 gap-y-1 sm:grid-cols-3">
      {METRICS.map(({ key, label, kind }) => {
        const value = metrics[key] ?? null
        return (
          <div key={key} className="flex items-baseline justify-between gap-2 text-xs">
            <dt className="text-slate-500">{label}</dt>
            <dd className={`tabular-nums ${SIGNED.has(key) ? statTone(value) : 'text-slate-200'}`}>
              {renderMetric(value, kind, SIGNED.has(key))}
            </dd>
          </div>
        )
      })}
    </dl>
  )
}

/**
 * The ledger half of a run: what it made, and how much of that is banked.
 *
 * Separate from `MetricGrid` above, and the separation is the point. Everything
 * in the metric grid is a float statistic over the return series; everything
 * here is money, arrives as a decimal string, and goes through `formatMoney`.
 * The two `total_return`s are the same quantity in the two types — the engine
 * computes one equity and reports it both ways — and mixing them would put a
 * ledger figure through the statistics formatter (CLAUDE.md §1.1).
 *
 * This exists because a return figure alone is readable two ways and only one
 * of them is a track record. The run that motivated it reported +202.8% of
 * which *none* was realised: twenty positions still open, `realized_pnl` zero,
 * the whole of it unrealised mark-to-market. Nothing on this screen could say
 * so — the nearest hint was `num_trades: 0` in the metric grid, which says
 * something different and reads as "does not trade much".
 */
function MoneyBlock({ totals }: { totals: BacktestTotalsView | null | undefined }) {
  if (!totals) {
    // Not zeros. A run stored before the server kept these computed them and
    // threw them away, and a nought here would be a figure nobody can check.
    return (
      <p className="text-xs text-slate-500">
        This run was recorded before the platform stored a run&rsquo;s money. Its return is in the
        metric set below; the realised and unrealised split it had is not recoverable.
      </p>
    )
  }

  const open = totals.open_positions
  return (
    <div className="space-y-2">
      <dl className="grid grid-cols-2 gap-x-6 gap-y-1 sm:grid-cols-3">
        <Figure label="Starting equity" value={totals.starting_equity} />
        <Figure label="Ending equity" value={totals.ending_equity} />
        <Figure label="Total return" value={totals.total_return} percent signed />
        <Figure label="…realised (closed trades)" value={totals.realized_pnl} signed />
        <Figure label="…unrealised (still open)" value={totals.unrealized_pnl} signed />
        <Figure label="Fees and commissions" value={totals.fees} />
      </dl>
      <p className="text-[11px] tabular-nums text-slate-500">
        {formatCount(totals.signals)} signals · {formatCount(totals.orders)} orders ·{' '}
        {formatCount(totals.filled_orders)} filled · {formatCount(open)} open at the end
      </p>
      {open > 0 ? (
        // The sentence `scripts/run_backtest.py` prints, on the screen that had
        // no way to say it. Above the metric grid, not below: the statistics
        // under it all count closed round trips, and this changes what they are
        // a statement about.
        <p className="rounded border border-sky-800/40 bg-sky-950/20 px-3 py-2 text-xs text-sky-200">
          {formatCount(open)} position{open === 1 ? '' : 's'} still open at the end, carrying{' '}
          {formatMoney(totals.unrealized_pnl)} of unrealised mark-to-market. That is part of the
          return above and part of none of the trade statistics below, which count closed round
          trips only.
        </p>
      ) : null}
    </div>
  )
}

/** One money figure. Decimal string in, `money.ts` formatter out — never a float. */
function Figure({
  label,
  value,
  percent = false,
  signed = false,
}: {
  label: string
  value: string
  percent?: boolean
  signed?: boolean
}) {
  return (
    <div className="flex items-baseline justify-between gap-2 text-xs">
      <dt className="text-slate-500">{label}</dt>
      <dd className={`tabular-nums ${signed ? toneFor(value) : 'text-slate-200'}`}>
        {percent ? formatPercent(value, { signed }) : formatMoney(value, { signed })}
      </dd>
    </div>
  )
}

interface Plotted {
  ts: string
  /** Pixels, not money — via `toChartNumber`, the one sanctioned conversion. */
  equity: number
  /** The exact string, so the tooltip never formats a float. */
  exact: string
}

function CurveTooltip({ active, payload }: { active?: boolean; payload?: { payload: Plotted }[] }) {
  const point = payload?.[0]?.payload
  if (!active || !point) return null
  return (
    <div className="rounded border border-slate-700 bg-slate-900 px-3 py-2 text-xs shadow">
      <div className="tabular-nums text-slate-100">{formatMoney(point.exact)}</div>
      <div className="text-slate-500">{formatDateTime(point.ts)}</div>
    </div>
  )
}

function Curve({ runId }: { runId: string }) {
  const { data, isLoading, error } = useBacktestCurve(runId, true)
  const points: Plotted[] = (data?.points ?? []).map((point) => ({
    ts: point[0] ?? '',
    equity: toChartNumber(point[1] ?? '0'),
    exact: point[1] ?? '0',
  }))

  if (isLoading) return <p className="py-8 text-center text-xs text-slate-500">Loading curve…</p>
  if (error) {
    return (
      <p className="py-8 text-center text-xs text-amber-400">Could not load the equity curve.</p>
    )
  }
  if (points.length === 0) {
    return <p className="py-8 text-center text-xs text-slate-500">This run recorded no curve.</p>
  }

  return (
    <div className="h-56">
      <ResponsiveContainer width="100%" height="100%">
        <AreaChart data={points} margin={{ top: 4, right: 8, bottom: 0, left: 8 }}>
          <defs>
            <linearGradient id="backtestFill" x1="0" y1="0" x2="0" y2="1">
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
          <Tooltip content={<CurveTooltip />} />
          <Area
            type="monotone"
            dataKey="equity"
            stroke="#38bdf8"
            strokeWidth={2}
            fill="url(#backtestFill)"
            isAnimationActive={false}
            dot={false}
          />
        </AreaChart>
      </ResponsiveContainer>
    </div>
  )
}

/** Tint per exit reason. The word is always present; colour is an accent. */
const EXIT_TONE: Record<string, string> = {
  stop_loss: 'text-amber-400',
  take_profit: 'text-emerald-400',
  signal: 'text-sky-400',
  time: 'text-slate-300',
  manual: 'text-slate-300',
  // The one that means the record does not say. Reachable in principle and worth
  // looking different from a real reason.
  unknown: 'text-slate-500',
}

function Trades({ runId }: { runId: string }) {
  const { data, isLoading, error } = useBacktestTrades(runId, true)
  const trades = (data?.trades ?? []) as BacktestTrade[]

  if (isLoading) return <p className="py-6 text-center text-xs text-slate-500">Loading trades…</p>
  if (error) {
    return <p className="py-6 text-center text-xs text-amber-400">Could not load the trades.</p>
  }
  if (trades.length === 0) {
    return (
      <p className="py-6 text-center text-xs text-slate-500">
        This run closed no round trips.
        {/* A result, not an absence — and a distinct one from "we have not
            reconstructed them". A position still open when the history ends is
            not a round trip, so a strategy that entered once and never exited
            legitimately reports nothing here. */}
        <span className="mt-1 block text-slate-600">
          A position still open when the window ends is not a round trip, so it is not listed.
        </span>
      </p>
    )
  }

  return (
    <div className="overflow-x-auto">
      <table className="w-full text-xs">
        <thead>
          <tr className="bg-slate-900/60 text-slate-500">
            <th className="px-2 py-1.5 text-left font-medium">Symbol</th>
            <th className="px-2 py-1.5 text-left font-medium">Side</th>
            <th className="px-2 py-1.5 text-right font-medium">Qty</th>
            <th className="px-2 py-1.5 text-right font-medium">Entry</th>
            <th className="px-2 py-1.5 text-right font-medium">Exit</th>
            <th className="px-2 py-1.5 text-right font-medium">Net P&amp;L</th>
            <th className="px-2 py-1.5 text-right font-medium">Held</th>
            <th className="px-2 py-1.5 text-left font-medium">Why it ended</th>
          </tr>
        </thead>
        <tbody>
          {trades.map((trade, index) => (
            <tr
              key={trade.trade_id ?? index}
              className="border-t border-slate-800/70 hover:bg-slate-800/30"
            >
              <td className="px-2 py-1.5 text-left font-medium text-slate-100">
                {trade.symbol ?? UNKNOWN}
              </td>
              <td className="px-2 py-1.5 text-left text-slate-400">{trade.side ?? UNKNOWN}</td>
              <td className="px-2 py-1.5 text-right tabular-nums text-slate-300">
                {trade.qty ? formatMoney(trade.qty, { places: 0 }) : UNKNOWN}
              </td>
              <td className="px-2 py-1.5 text-right tabular-nums text-slate-300">
                {formatMoney(trade.entry_price)}
                <div className="text-[10px] text-slate-600">{formatDateTime(trade.entry_ts)}</div>
              </td>
              <td className="px-2 py-1.5 text-right tabular-nums text-slate-300">
                {formatMoney(trade.exit_price)}
                <div className="text-[10px] text-slate-600">{formatDateTime(trade.exit_ts)}</div>
              </td>
              <td className={`px-2 py-1.5 text-right tabular-nums ${toneFor(trade.net_pnl)}`}>
                {formatMoney(trade.net_pnl, { signed: true })}
                <div className="text-[10px] text-slate-600">fees {formatMoney(trade.fees)}</div>
              </td>
              <td className="px-2 py-1.5 text-right tabular-nums text-slate-400">
                {formatDuration(trade.holding_period_hours)}
              </td>
              <td className="px-2 py-1.5 text-left">
                <span className={EXIT_TONE[trade.exit_reason ?? ''] ?? 'text-slate-400'}>
                  {trade.exit_reason ?? UNKNOWN}
                </span>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

export default function BacktestDetail({ run, onClose }: Props) {
  const finished = run.status === 'done'

  return (
    <section className="rounded border border-slate-700 bg-slate-900/40">
      <div className="flex flex-wrap items-baseline justify-between gap-2 border-b border-slate-800 px-4 py-3">
        <div>
          <h2 className="text-sm font-semibold text-slate-200">
            {run.strategy_id} · {(run.spec.symbols ?? []).join(', ')}
          </h2>
          <p className="mt-0.5 text-xs text-slate-500">
            {run.spec.start.slice(0, 10)} → {run.spec.end.slice(0, 10)} · {run.spec.timeframe} ·{' '}
            {formatMoney(run.spec.starting_cash, { places: 0 })} · {run.spec.qty} shares per entry ·{' '}
            {run.spec.cost_model}
          </p>
          <p className="mt-0.5 text-[11px] text-slate-600">
            queued {formatDateTime(run.queued_at)}
            {run.started_at ? ` · started ${formatDateTime(run.started_at)}` : ''}
            {run.finished_at ? ` · finished ${formatDateTime(run.finished_at)}` : ''}
          </p>
        </div>
        <button
          type="button"
          onClick={onClose}
          className="rounded border border-slate-700 px-2 py-1 text-xs text-slate-400 hover:text-slate-200"
        >
          Close
        </button>
      </div>

      {(run.warnings ?? []).length > 0 ? (
        // Above the numbers, not below. A number a reader has already seen is a
        // number they have already believed.
        <ul className="border-b border-amber-800/40 bg-amber-950/20 px-4 py-2 text-xs text-amber-200">
          {(run.warnings ?? []).map((warning) => (
            <li key={warning}>⚠ {warning}</li>
          ))}
        </ul>
      ) : null}

      {run.status === 'failed' ? (
        <div className="px-4 py-6 text-sm text-amber-300">
          This run did not produce a result.
          <p className="mt-1 text-xs text-amber-200/80">
            {run.error ?? 'No reason was recorded, which is itself a bug worth reporting.'}
          </p>
        </div>
      ) : !finished ? (
        <div className="px-4 py-6 text-sm text-slate-400">
          {run.status === 'queued'
            ? 'Waiting for a worker to pick this up.'
            : 'Running. Metrics, the equity curve and the trades appear when it finishes.'}
          {run.progress ? (
            <p className="mt-1 text-xs text-slate-500 tabular-nums">
              {formatCount(run.progress.bars_done)} of {formatCount(run.progress.bars_total)} bars ·
              reported {formatDateTime(run.progress.at)}
            </p>
          ) : null}
        </div>
      ) : (
        <div className="space-y-4 p-4">
          <div>
            {/* "Money" rather than "Result", and not only because the run list
                already has a Result column: the pairing with "Metrics" below is
                the distinction this panel exists to make — decimal strings
                through `money.ts` here, float statistics through `stats.ts`
                there. */}
            <h3 className="mb-2 text-xs font-semibold uppercase tracking-wide text-slate-500">
              Money
            </h3>
            <MoneyBlock totals={run.totals} />
          </div>

          <div>
            <h3 className="mb-2 text-xs font-semibold uppercase tracking-wide text-slate-500">
              Metrics
            </h3>
            <MetricGrid metrics={run.metrics ?? {}} />
            <p className="mt-2 text-[11px] text-slate-600">
              These are float statistics over the return series, not ledger figures — including the
              five denominated in account currency. A blank is a metric with no value (an infinite
              profit factor means nothing lost, which is too few trades rather than perfection).
            </p>
          </div>

          <div>
            <h3 className="mb-2 text-xs font-semibold uppercase tracking-wide text-slate-500">
              Equity
            </h3>
            <Curve runId={run.id} />
          </div>

          <div>
            <h3 className="mb-2 text-xs font-semibold uppercase tracking-wide text-slate-500">
              Trades
            </h3>
            <Trades runId={run.id} />
            <p className="mt-2 text-[11px] text-slate-600">
              Inspecting individual trades is how you catch a backtest that is profitable because of
              one impossible fill. Exit reasons come from the order that closed each position, so a
              stop-out and a signal exit are told apart.
            </p>
          </div>
        </div>
      )}
    </section>
  )
}
