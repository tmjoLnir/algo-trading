/**
 * Live held up against one stored backtest — the report docs/ANALYTICS.md calls
 * the most important one here, and the one most easily read into saying
 * something it does not.
 *
 * The endpoint has existed since #68 with nothing calling it. This is that half,
 * and four things about it are the whole design rather than presentation:
 *
 * **A run picker, not a date range.** Every other panel on this page is a
 * period a reader chose. This one turns on *which backtest*, because a strategy
 * accumulates any number of stored runs over different windows, cost models and
 * share counts — and comparing live against an arbitrary one reports a
 * divergence against a backtest nobody used to approve anything. The picker
 * therefore starts empty rather than defaulting to the newest run: defaulting
 * would be this screen making exactly the choice the endpoint refuses to make.
 *
 * **`comparability` renders beside every row, and it is not optional.** Five of
 * the nineteen metrics are annualised and four more scale with the window, so a
 * divergence on those rows is partly measurement rather than performance. A
 * table of nineteen bare subtractions is the misreading the response was built
 * to prevent, and rendering one would undo the endpoint's work at the last step.
 *
 * **A null divergence is a dash, never a zero.** Zero is the strongest claim
 * this report can make — live matched the backtest exactly — and it is the last
 * thing an absent value should render as. The absences are routine: a stored run
 * nulls its non-finite metrics on the way into its JSON column, and an infinite
 * `profit_factor` means the backtest had no losing trade, which is precisely the
 * run somebody holds a live record up against.
 *
 * **No verdict column, on the same principle as `BacktestComparison`.** The
 * sign of a divergence is not a judgement on most of these rows: more
 * volatility, more round trips, longer holds and higher turnover are differences
 * rather than failures, and colouring them green or red would be this screen
 * ruling on questions the numbers do not answer. The reader gets the signed
 * difference, what basis it sits on, and the warnings — and makes the call.
 *
 * Metric names are the server's own, not friendlier labels, deliberately: this
 * table is read beside the API response and `METRIC_BASIS`, and a reader
 * checking one against the other should not have to translate.
 */

import { formatDateTime, UNKNOWN } from '@/lib/money'
import { formatCount, formatMetric, formatMetricDelta, formatStat } from '@/lib/stats'
import type { BacktestSideView, ComparisonWindowView, LiveVsBacktestResponse } from '@/api/types'

interface Props {
  data: LiveVsBacktestResponse
  /** Pin both sides to the backtest's own basis, or let the server infer live's. */
  pinned: boolean
  onPinnedChange: (pinned: boolean) => void
}

/**
 * What each basis means, in a phrase short enough to sit in a table cell.
 *
 * `per_trade` is the good news and is worth saying so: those are the metrics two
 * records of different shapes can be compared on directly, and where a real
 * divergence shows up first.
 */
const BASIS_LABEL: Record<string, string> = {
  per_trade: 'per trade',
  annualised: 'annualised',
  window: 'window',
}

const BASIS_HINT: Record<string, string> = {
  per_trade:
    'Comparable directly. A per-trade statistic does not scale with the window or the annualisation basis, so a divergence here is about the strategy.',
  annualised:
    'Scaled by periods-per-year. Differs between two series annualised on different bases before the strategy has done anything — pin the basis above to remove that part.',
  window:
    'A property of the curve over its own window. A live month and a backtested five years do not produce comparable values even when the strategy behaved identically.',
}

const BASIS_TONE: Record<string, string> = {
  per_trade: 'text-slate-400',
  annualised: 'text-amber-400/80',
  window: 'text-amber-400/80',
}

function windowText(window: ComparisonWindowView): string {
  if (!window.start || !window.end) return 'nothing closed'
  const days =
    window.days === null || window.days === undefined
      ? null
      : formatStat(window.days, { places: 1 })
  return `${formatDateTime(window.start)} → ${formatDateTime(window.end)}${days ? ` · ${days}d` : ''}`
}

function Fact({
  label,
  value,
  tone = 'text-slate-300',
}: {
  label: string
  value: string
  tone?: string
}) {
  return (
    <div>
      <dt className="text-[11px] uppercase tracking-wide text-slate-500">{label}</dt>
      <dd className={`mt-0.5 tabular-nums ${tone}`}>{value}</dd>
    </div>
  )
}

/**
 * The stored run's identity, beside its numbers.
 *
 * Cost model, share count, timeframe and symbols are what make two runs of the
 * same strategy different results, so they belong on screen with the
 * comparison rather than one click away on another tab. `zero` cost is called
 * out in amber for the reason the backtest form calls it out: a zero-cost result
 * is not evidence about a strategy, and a divergence against one is a
 * divergence against a number that was never real.
 */
function BacktestIdentity({ backtest }: { backtest: BacktestSideView }) {
  return (
    <dl className="grid grid-cols-2 gap-x-4 gap-y-2 text-xs sm:grid-cols-3 lg:grid-cols-6">
      <Fact label="Run" value={backtest.run_id} />
      <Fact label="Symbols" value={backtest.symbols.join(', ') || UNKNOWN} />
      <Fact label="Timeframe" value={backtest.timeframe} />
      <Fact
        label="Cost model"
        value={backtest.cost_model}
        tone={backtest.cost_model === 'zero' ? 'text-amber-400' : 'text-slate-300'}
      />
      <Fact label="Sizing" value={`${backtest.qty} sh/entry`} />
      <Fact label="Finished" value={formatDateTime(backtest.finished_at)} />
    </dl>
  )
}

export default function LiveVsBacktest({ data, pinned, onPinnedChange }: Props) {
  const { live, backtest, divergence, comparability, warnings } = data
  const basisAgrees = live.periods_per_year === backtest.periods_per_year

  // **Ordered by `comparability`, not by `divergence`.** The server builds the
  // divergence map by iterating a *set union* of the two metric sets, so its key
  // order is arbitrary and need not survive a restart — and a table whose
  // nineteen rows reshuffled between two reads of the same run would be unusable
  // and unscreenshottable. `comparability` is a plain dict over `METRIC_BASIS`,
  // so its order is that declaration's: returns, then risk, then the per-trade
  // statistics, which is the order docs/ANALYTICS.md discusses them in.
  //
  // Anything subtracted but unclassified — a metric stored before the basis map
  // grew it — is appended rather than dropped. Dropping it would hide the one
  // row whose meaning nobody has decided yet.
  const rows = [
    ...Object.keys(comparability),
    ...Object.keys(divergence).filter((name) => !(name in comparability)),
  ]

  return (
    <div className="space-y-3">
      {/* Above the numbers, not below them: a number a reader has already seen
          is a number they have already believed. The endpoint computes these
          server-side on that same principle. */}
      {warnings.length > 0 ? (
        <ul className="space-y-1 rounded border border-amber-800/40 bg-amber-950/20 px-4 py-3 text-xs text-amber-200">
          {warnings.map((warning) => (
            <li key={warning}>⚠ {warning}</li>
          ))}
        </ul>
      ) : null}

      {backtest.warnings.length > 0 ? (
        <ul className="space-y-1 rounded border border-amber-800/30 bg-amber-950/10 px-4 py-3 text-xs text-amber-200/80">
          {backtest.warnings.map((warning) => (
            <li key={warning}>⚠ About the backtest itself: {warning}</li>
          ))}
        </ul>
      ) : null}

      <div className="rounded border border-slate-800 bg-slate-900/30 px-4 py-3">
        <BacktestIdentity backtest={backtest} />
      </div>

      <dl className="grid gap-3 sm:grid-cols-2">
        <div className="rounded border border-slate-800 bg-slate-900/30 px-4 py-3">
          <h3 className="mb-2 text-xs font-semibold uppercase tracking-wide text-slate-400">
            Live · {live.strategy_id}
          </h3>
          <div className="grid grid-cols-2 gap-x-4 gap-y-2 text-xs">
            <Fact label="Round trips" value={formatCount(live.num_trades)} />
            <Fact label="Symbols" value={live.symbols.join(', ') || UNKNOWN} />
            <Fact label="Covered" value={windowText(live.window)} />
            <Fact label="Annualised at" value={`${formatCount(live.periods_per_year)}/yr`} />
          </div>
        </div>
        <div className="rounded border border-slate-800 bg-slate-900/30 px-4 py-3">
          <h3 className="mb-2 text-xs font-semibold uppercase tracking-wide text-slate-400">
            Backtest
          </h3>
          <div className="grid grid-cols-2 gap-x-4 gap-y-2 text-xs">
            <Fact
              label="Round trips"
              value={formatMetric('num_trades', backtest.metrics.num_trades)}
            />
            <Fact label="Starting cash" value={backtest.starting_cash} />
            <Fact label="Covered" value={windowText(backtest.window)} />
            <Fact label="Annualised at" value={`${formatCount(backtest.periods_per_year)}/yr`} />
          </div>
        </div>
      </dl>

      {/* The actionable half of the annualisation warning. Offered as a control
          rather than applied by default, because the inferred basis is the
          honest description of a curve that steps once per closed trade —
          pinning is a question the reader asks, not a correction the screen
          makes on their behalf. */}
      <label className="flex flex-wrap items-center gap-2 text-xs text-slate-400">
        <input
          type="checkbox"
          checked={pinned}
          onChange={(event) => onPinnedChange(event.target.checked)}
          className="rounded border-slate-700 bg-slate-900"
        />
        Pin both sides to the backtest&apos;s basis ({backtest.periods_per_year}/yr)
        <span className="text-slate-600">
          {basisAgrees
            ? '— both sides are on one basis, so annualised rows are comparable'
            : `— live is inferred at ${live.periods_per_year}/yr, so every annualised row differs partly for that reason`}
        </span>
      </label>

      <div className="overflow-x-auto rounded border border-slate-800">
        <table className="w-full text-xs">
          <thead>
            <tr className="bg-slate-900/60 text-slate-500">
              <th className="px-3 py-2 text-left font-medium">Metric</th>
              <th className="px-3 py-2 text-right font-medium">Live</th>
              <th className="px-3 py-2 text-right font-medium">Backtest</th>
              <th className="px-3 py-2 text-right font-medium">Divergence</th>
              <th className="px-3 py-2 text-left font-medium">Comparable on</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((name) => {
              const basis = comparability[name] ?? 'ratio'
              return (
                <tr key={name} className="border-t border-slate-800/70">
                  <td className="px-3 py-1.5 text-left text-slate-400">{name}</td>
                  <td className="px-3 py-1.5 text-right tabular-nums text-slate-200">
                    {formatMetric(name, live.metrics[name])}
                  </td>
                  <td className="px-3 py-1.5 text-right tabular-nums text-slate-200">
                    {formatMetric(name, backtest.metrics[name])}
                  </td>
                  {/* Uncoloured on purpose — see this file's header. The dash is
                      "not available", which is a different fact from a zero and
                      must never be shown as one. */}
                  <td className="px-3 py-1.5 text-right tabular-nums text-slate-100">
                    {formatMetricDelta(name, divergence[name])}
                  </td>
                  <td
                    className={`px-3 py-1.5 text-left ${BASIS_TONE[basis] ?? 'text-slate-400'}`}
                    title={BASIS_HINT[basis]}
                  >
                    {BASIS_LABEL[basis] ?? basis}
                  </td>
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>

      <p className="text-[11px] text-slate-600">
        Divergence is <span className="text-slate-500">live − backtest</span>, and no row is marked
        good or bad. On most of them the sign is a difference rather than a verdict — more
        volatility, more round trips or a longer hold is not a failure — so the reading is left to
        you. A dash is <span className="text-slate-500">not available</span>, never zero: zero would
        claim live matched the backtest exactly.
      </p>
      <p className="text-[11px] text-slate-600">
        Nothing records which backtest a promotion was granted against (ADR 0010), so this cannot
        tell you whether the run above is the representative one. Naming an unrepresentative run
        gives an answer that is arithmetically correct and worthless.
      </p>
    </div>
  )
}
